"""
Monitor de filas Disney/Universal Orlando.
Fonte de dados: Queue-Times.com (Powered by Queue-Times.com — https://queue-times.com)

Modos de operação (automáticos, por data):
- COLETA: fora das datas da viagem, apenas grava histórico no SQLite.
- ALERTA: nos dias de parque (park_days), além de gravar, envia alertas
  no Telegram quando uma atração da watchlist cai abaixo do threshold.

Nos dois modos o bot atende comandos no Telegram (/status, /parques, /help)
enquanto espera o próximo ciclo de coleta.
"""
import json
import logging
import sqlite3
import time
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import localizacao  # noqa: E402 — ciclo resolvido: uso só dentro de funções
import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("monitor")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "history.db"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
# data/ é o único volume persistente: coords.json gravado em /app some no
# próximo `docker compose up --build`, junto com o trabalho todo do coords.py.
COORDS_PATH = BASE_DIR / "data" / "coords.json"
COORDS_PATH_REPO = BASE_DIR / "coords.json"  # versionado no git, se houver

PARKS_URL = "https://queue-times.com/parks.json"
QUEUE_URL = "https://queue-times.com/parks/{park_id}/queue_times.json"

POLL_INTERVAL_SECONDS = 300  # 5 min — mesma frequência de atualização da API
COMMAND_POLL_SECONDS = 20    # long polling do Telegram dentro da espera entre ciclos
HTTP_TIMEOUT = 15
HTTP_TENTATIVAS = 3          # a API cai por segundos; desistir na primeira perde o ciclo
HTTP_BACKOFF_BASE = 2        # espera 2s, 4s entre tentativas


# ---------------------------------------------------------------- config

def load_config() -> dict:
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def validar_config(config: dict) -> list[str]:
    """Problemas de configuração que fariam o monitor falhar calado.

    Devolve lista de mensagens; vazia quer dizer config sã. Só valida o que tem
    consequência silenciosa — nome de parque errado já vira warning na resolução.
    """
    problemas = []
    if not config.get("parks"):
        problemas.append("watchlist.json sem nenhum parque em 'parks'")
    if not config.get("trip", {}).get("timezone"):
        problemas.append("trip.timezone ausente — sem ele não dá para saber a hora do parque")
    for dia, parques in config.get("park_days", {}).items():
        try:
            date.fromisoformat(dia)
        except ValueError:
            problemas.append(f"park_days: {dia!r} não é uma data ISO (AAAA-MM-DD)")
        for parque in parques:
            if parque not in config.get("parks", {}):
                problemas.append(f"park_days {dia}: {parque!r} não existe em 'parks'")
    quiet = config.get("alert", {}).get("quiet_hours") or {}
    for campo in ("start", "end"):
        valor = quiet.get(campo)
        if valor is not None and not re.fullmatch(r"\d{2}:\d{2}", valor):
            problemas.append(f"alert.quiet_hours.{campo} = {valor!r} não está em HH:MM")
    hora_resumo = config.get("daily_summary", {}).get("hour")
    if hora_resumo is not None and not re.fullmatch(r"\d{2}:\d{2}", hora_resumo):
        problemas.append(f"daily_summary.hour = {hora_resumo!r} não está em HH:MM")
    return problemas


def utc_now() -> datetime:
    """Agora em UTC, sem tzinfo.

    datetime.utcnow() está deprecado no 3.12, mas o .replace(tzinfo=None) é de
    propósito: o banco já tem histórico gravado sem offset e misturar os dois
    formatos na mesma coluna bagunçaria o strftime do analyze.py.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- db

def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wait_times (
            ts        TEXT NOT NULL,          -- ISO UTC
            park      TEXT NOT NULL,
            land      TEXT,
            ride      TEXT NOT NULL,
            wait_time INTEGER,
            is_open   INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wait_park_ride_ts ON wait_times (park, ride, ts)"
    )
    conn.execute(  # resumo e tendência varrem por parque+tempo, sem filtrar ride
        "CREATE INDEX IF NOT EXISTS idx_wait_park_ts ON wait_times (park, ts)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts_sent (
            park    TEXT NOT NULL,
            ride    TEXT NOT NULL,
            sent_at TEXT NOT NULL            -- ISO UTC
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS top_alert (
            id      INTEGER PRIMARY KEY CHECK (id = 1),   -- linha única
            sent_at TEXT NOT NULL                         -- ISO UTC do último envio
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_summary (
            sent_on TEXT PRIMARY KEY          -- data no fuso do parque
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------- api

def resolve_park_ids(wanted_names: list[str]) -> dict[str, int]:
    """Resolve nomes de parques em IDs consultando parks.json (nunca hardcode)."""
    groups = get_json(PARKS_URL)
    if not isinstance(groups, list):
        raise RespostaInvalida("parks.json não veio como lista")

    available: dict[str, int] = {}
    for group in groups:
        for park in group.get("parks", []):
            available[park["name"].strip().lower()] = park["id"]

    resolved: dict[str, int] = {}
    for name in wanted_names:
        key = name.strip().lower()
        if key in available:
            resolved[name] = available[key]
            continue
        # fallback: match parcial (ex.: "Epcot" dentro de "Epcot Theme Park")
        matches = [pname for pname in available if key in pname or pname in key]
        if len(matches) == 1:
            resolved[name] = available[matches[0]]
        elif matches:
            log.warning(
                "Parque ambíguo na API: %r casa com %s — use o nome exato no watchlist.json",
                name, matches,
            )
        else:
            log.warning(
                "Parque não resolvido na API: %r. Nomes parecidos disponíveis: %s "
                "— copie o exato para o watchlist.json",
                name, suggest_park_names(key, available) or "(nenhum)",
            )
    return resolved


def suggest_park_names(key: str, available: dict[str, int], limit: int = 5) -> list[str]:
    """Nomes da API que dividem palavras com o procurado.

    Existe para o log de parque não resolvido ser acionável: sem isso ele só
    dizia "matches=[]" e você tinha que ir garimpar o parks.json na mão.
    """
    tokens = {t for t in key.split() if len(t) > 3}
    scored = []
    for pname in available:
        comuns = len(tokens & {t for t in pname.split() if len(t) > 3})
        if comuns:
            scored.append((comuns, pname))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [pname for _, pname in scored[:limit]]


class RespostaInvalida(requests.RequestException):
    """JSON veio, mas não no formato que esperamos — tratado como falha de rede."""


def _dormir(segundos: float) -> None:
    time.sleep(segundos)  # isolado para o teste conseguir substituir


USER_AGENT = ("Fila-Disney/1.0 (monitor de filas de parques; "
              "https://github.com/haohmarusc-glitch/Fila-Disney-)")


def get_json(url: str, *, tentativas: int = HTTP_TENTATIVAS) -> object:
    """GET com retry e backoff. Respeita Retry-After no 429."""
    return requisicao_json("GET", url, tentativas=tentativas)


def post_json(url: str, dados: dict, *, tentativas: int = HTTP_TENTATIVAS,
              espera_minima: float = 0) -> object:
    """POST form-encoded. A Overpass exige POST para consulta e recusa GET longo."""
    return requisicao_json("POST", url, dados=dados, tentativas=tentativas,
                           espera_minima=espera_minima)


def post_json_body(url: str, dados: dict, *, cabecalhos: dict | None = None,
                   tentativas: int = HTTP_TENTATIVAS) -> object:
    """POST JSON para APIs modernas, preservando retry/backoff centralizados."""
    return requisicao_json("POST_JSON", url, json_dados=dados,
                           cabecalhos_extras=cabecalhos, tentativas=tentativas)


def requisicao_json(metodo: str, url: str, *, dados: dict | None = None,
                    json_dados: dict | None = None,
                    cabecalhos_extras: dict | None = None,
                    tentativas: int = HTTP_TENTATIVAS,
                    espera_minima: float = 0) -> object:
    """Núcleo HTTP com retry e backoff.

    Um ciclo perdido é histórico perdido para sempre, então vale insistir um
    pouco. Erro 4xx que não seja 429 não é retentado: não vai melhorar sozinho.
    Manda User-Agent identificável — a Overpass devolve 406 para o padrão do
    python-requests.
    """
    ultimo_erro: Exception | None = None
    cabecalhos = {"User-Agent": USER_AGENT, **(cabecalhos_extras or {})}
    for tentativa in range(1, tentativas + 1):
        try:
            if metodo == "POST_JSON":
                resp = requests.post(url, json=json_dados, headers=cabecalhos,
                                     timeout=HTTP_TIMEOUT)
            elif metodo == "POST":
                resp = requests.post(url, data=dados, headers=cabecalhos, timeout=HTTP_TIMEOUT)
            else:
                resp = requests.get(url, headers=cabecalhos, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                espera = max(
                    float(resp.headers.get("Retry-After") or HTTP_BACKOFF_BASE ** tentativa),
                    espera_minima,  # a Overpass libera slot em dezenas de segundos
                )
                log.warning("429 em %s — aguardando %.0fs", url, espera)
                ultimo_erro = requests.HTTPError("429 Too Many Requests")
                if tentativa < tentativas:
                    _dormir(espera)
                continue
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()  # não adianta repetir
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            ultimo_erro = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                break
            # "Network is unreachable" e DNS quebrado não melhoram esperando: o
            # espera_minima existe para servidor pedindo calma, não para rota
            # inexistente. Insistir 45s aqui só empata a execução.
            if isinstance(exc, ConnectionError) or isinstance(
                    exc, getattr(requests, "ConnectionError", ())):
                if tentativa >= 2:
                    break
                espera = HTTP_BACKOFF_BASE
            elif tentativa < tentativas:
                espera = max(HTTP_BACKOFF_BASE ** tentativa, espera_minima)
            if tentativa < tentativas:
                log.warning("Falha em %s (tentativa %d/%d): %s — nova tentativa em %ds",
                            url, tentativa, tentativas, exc, espera)
                _dormir(espera)
    raise requests.RequestException(f"{url}: {ultimo_erro}") from ultimo_erro


def fetch_queue_times(park_id: int) -> dict:
    payload = get_json(QUEUE_URL.format(park_id=park_id))
    if not isinstance(payload, dict) or not ("lands" in payload or "rides" in payload):
        raise RespostaInvalida(f"parque {park_id}: JSON sem 'lands' nem 'rides'")
    return payload


def iter_rides(payload: dict):
    """Gera (land, ride_dict) — a API retorna rides dentro de lands e/ou na raiz."""
    for land in payload.get("lands", []):
        for ride in land.get("rides", []):
            yield land.get("name"), ride
    for ride in payload.get("rides", []):
        yield None, ride


# ---------------------------------------------------------------- alertas

def now_park(config: dict) -> datetime:
    return datetime.now(ZoneInfo(config["trip"]["timezone"]))


def is_alert_day(config: dict) -> list[str]:
    """Retorna a lista de parques do dia (vazia = modo coleta)."""
    today = now_park(config).date().isoformat()
    return config.get("park_days", {}).get(today, [])


def in_quiet_hours(config: dict) -> bool:
    quiet = config.get("alert", {}).get("quiet_hours")
    if not quiet:
        return False
    now = now_park(config).strftime("%H:%M")
    start, end = quiet["start"], quiet["end"]
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # janela cruzando meia-noite


def recently_alerted(conn: sqlite3.Connection, park: str, ride: str, cooldown_min: int) -> bool:
    cutoff = (utc_now() - timedelta(minutes=cooldown_min)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE park = ? AND ride = ? AND sent_at > ? LIMIT 1",
        (park, ride, cutoff),
    ).fetchone()
    return row is not None


def mark_alerted(conn: sqlite3.Connection, park: str, ride: str) -> None:
    conn.execute(
        "INSERT INTO alerts_sent (park, ride, sent_at) VALUES (?, ?, ?)",
        (park, ride, utc_now().isoformat()),
    )
    conn.commit()


# Filas paralelas que a API publica como atração separada. O match parcial da
# watchlist casa com elas ("Test Track" dentro de "Test Track Presented by
# Chevrolet Single Rider") e elas reportam 0 min quando não há dado — o que
# viraria alerta falso de "0 min, vai agora" logo no primeiro ciclo do dia.
# Continuam sendo gravadas no histórico; ficam fora só de alerta e /status.
FILAS_IGNORADAS = ("single rider", "virtual line", "virtual queue")

# A API entrega last_updated por atração. Leitura parada há muito tempo é número
# velho: alertar "20 min" com dado de 3h atrás manda o grupo para uma fila que
# não existe mais. Só afeta alerta e ranking — o histórico continua gravando tudo.
OBSOLETO_MINUTOS_PADRAO = 30


def leitura_obsoleta(ride: dict, limite_min: int = OBSOLETO_MINUTOS_PADRAO) -> bool:
    """True se o last_updated da atração for mais velho que o limite."""
    bruto = ride.get("last_updated")
    if not bruto:
        return False  # sem o campo não dá para julgar: vale o dado
    try:
        marca = datetime.fromisoformat(str(bruto).replace("Z", "+00:00"))
    except ValueError:
        return False
    if marca.tzinfo is not None:
        marca = marca.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_now() - marca > timedelta(minutes=limite_min)


def fila_paralela(ride_name: str) -> bool:
    """True para single rider / fila virtual — entrada separada na API, tempo 0."""
    return any(termo in ride_name.lower() for termo in FILAS_IGNORADAS)


def get_threshold(park_cfg: dict, ride_name: str) -> int | None:
    """Threshold da atração; None se não estiver na watchlist do parque."""
    if fila_paralela(ride_name):
        return None
    attractions = park_cfg.get("attractions", {})
    for watched, threshold in attractions.items():
        if watched.lower() in ride_name.lower() or ride_name.lower() in watched.lower():
            return threshold
    return None


# ---------------------------------------------------------------- tendência

JANELA_TENDENCIA_MIN = 35   # ~7 ciclos de 5 min
DELTA_TENDENCIA_MIN = 5     # variação menor que isso é ruído da própria API


def tendencia(conn: sqlite3.Connection, park: str, ride: str) -> tuple[str, int] | None:
    """(seta, variação em min) comparando a fila de agora com ~35 min atrás.

    Existe porque "31 min e subindo" e "31 min e caindo" são decisões opostas
    dentro do parque, e o threshold sozinho não distingue as duas.
    """
    corte = (utc_now() - timedelta(minutes=JANELA_TENDENCIA_MIN)).isoformat()
    linhas = conn.execute(
        """
        SELECT ts, wait_time FROM wait_times
        WHERE park = ? AND ride = ? AND is_open = 1 AND wait_time IS NOT NULL AND ts >= ?
        ORDER BY ts
        """,
        (park, ride, corte),
    ).fetchall()
    if len(linhas) < 2:
        return None
    variacao = linhas[-1][1] - linhas[0][1]
    if variacao <= -DELTA_TENDENCIA_MIN:
        return "↓", variacao
    if variacao >= DELTA_TENDENCIA_MIN:
        return "↑", variacao
    return "→", variacao


def marca_tendencia(conn: sqlite3.Connection | None, park: str, ride: str) -> str:
    """Sufixo pronto para a mensagem, ou string vazia se não há dado."""
    if conn is None:
        return ""
    resultado = tendencia(conn, park, ride)
    if resultado is None:
        return ""
    seta, variacao = resultado
    if seta == "→":
        return " →"
    return f" {seta}{abs(variacao)}"


# ---------------------------------------------------------------- menores filas

def menores_filas(payload: dict, config: dict, park_name: str, limite: int,
                  apenas_watchlist: bool) -> list[tuple[int, str, int | None]]:
    """(espera, atração, threshold) das menores filas abertas, da menor para a maior.

    Filas paralelas ficam de fora: reportam 0 min sem dado e ocupariam o topo do
    ranking inteiro, que é justamente o que o comando existe para mostrar.
    """
    park_cfg = config["parks"].get(park_name, {})
    limite_obsoleto = config.get("alert", {}).get("max_staleness_minutes", OBSOLETO_MINUTOS_PADRAO)
    abertas = []
    for _land, ride in iter_rides(payload):
        nome = ride["name"]
        if fila_paralela(nome):
            continue
        if not ride.get("is_open"):
            continue
        if leitura_obsoleta(ride, limite_obsoleto):
            continue
        wait = ride.get("wait_time")
        if wait is None:
            continue
        threshold = get_threshold(park_cfg, nome)
        if apenas_watchlist and threshold is None:
            continue
        abertas.append((wait, nome, threshold))
    abertas.sort(key=lambda item: (item[0], item[1]))
    return abertas[:limite]


def format_menores(park_name: str, payload: dict, config: dict, limite: int) -> str:
    """Ranking das menores filas do parque inteiro — inclui o que não é watchlist."""
    ranking = menores_filas(payload, config, park_name, limite, apenas_watchlist=False)
    if not ranking:
        return f"📉 <b>{notifier.esc(park_name)}</b>\n\nNenhuma atração aberta agora."

    agora = now_park(config).strftime("%Hh%M")
    linhas = [
        f"📉 <b>Menores filas — {notifier.esc(park_name)}</b>",
        f"🕒 {agora} no horário do parque",
        "",
    ]
    for wait, ride, threshold in ranking:
        marca = "✅" if threshold is not None and wait <= threshold else "▫️"
        estrela = " ⭐" if threshold is not None else ""   # está na sua watchlist
        linhas.append(f"{marca} <b>{wait} min</b> · {notifier.esc(ride)}{estrela}")
    linhas += ["", "⭐ = está na sua watchlist", "Powered by Queue-Times.com"]
    return "\n".join(linhas)


def format_top_alert(park_name: str, ranking: list, config: dict,
                     conn: sqlite3.Connection | None = None) -> str:
    """Mensagem curta do alerta recorrente — chega a cada N min, tem que ser enxuta."""
    agora = now_park(config).strftime("%Hh%M")
    medalhas = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
    linhas = [f"⚡ <b>Menores filas agora</b> · {notifier.esc(park_name)} · {agora}"]
    for i, (wait, ride, threshold) in enumerate(ranking):
        marca = " ✅" if threshold is not None and wait <= threshold else ""
        medalha = medalhas[i] if i < len(medalhas) else "•"
        seta = marca_tendencia(conn, park_name, ride)
        linhas.append(f"{medalha} {notifier.esc(ride)} — <b>{wait} min</b>{seta}{marca}")
    return "\n".join(linhas)


def top_alert_atrasado(conn: sqlite3.Connection, intervalo_min: int) -> bool:
    """True se já passou o intervalo desde o último envio (ou se nunca houve)."""
    row = conn.execute("SELECT sent_at FROM top_alert WHERE id = 1").fetchone()
    if not row:
        return True
    return datetime.fromisoformat(row[0]) <= utc_now() - timedelta(minutes=intervalo_min)


def marcar_top_alert(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO top_alert (id, sent_at) VALUES (1, ?)", (utc_now().isoformat(),)
    )
    conn.commit()


def maybe_send_top_alert(conn: sqlite3.Connection, config: dict, park_ids: dict[str, int],
                         payloads: dict[str, dict]) -> None:
    """Manda as N menores filas do parque do dia, a cada N minutos."""
    cfg = config.get("top_alert", {})
    if not cfg.get("enabled", False):
        return
    if in_quiet_hours(config):
        return

    do_dia = [p for p in is_alert_day(config) if p in park_ids]
    if not do_dia and cfg.get("only_park_days", True):
        return
    park_name = (do_dia or list(park_ids))[0]

    payload = payloads.get(park_name)
    if payload is None:  # o fetch deste parque falhou neste ciclo
        return
    if not top_alert_atrasado(conn, cfg.get("every_minutes", 10)):
        return

    ranking = menores_filas(
        payload, config, park_name, cfg.get("count", 3), apenas_watchlist=True
    )
    if not ranking:
        return
    if notifier.send(format_top_alert(park_name, ranking, config, conn)):
        marcar_top_alert(conn)
        log.info("Top-%d de menores filas enviado (%s)", len(ranking), park_name)


# ---------------------------------------------------------------- resumo diário

JANELA_RESUMO_MINUTOS = 120  # se o container subiu tarde, o resumo ainda vale
HORAS_PARQUE = range(8, 23)  # fora disso a média é ruído de parque fechado


def hhmm_em_minutos(hhmm: str) -> int:
    horas, minutos = hhmm.split(":")
    return int(horas) * 60 + int(minutos)


def park_utc_offset_horas(config: dict) -> int:
    """Offset do fuso do parque agora: -4 no EDT, -5 no EST.

    Calculado, não fixo: o histórico é lido em hora UTC e deslocado, e em
    novembro Orlando volta para EST — offset fixo erraria a leitura em 1h.
    """
    delta = now_park(config).utcoffset()
    return int(delta.total_seconds() // 3600) if delta else 0


def resumo_enviado(conn: sqlite3.Connection, dia: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM daily_summary WHERE sent_on = ? LIMIT 1", (dia,)
    ).fetchone() is not None


def marcar_resumo_enviado(conn: sqlite3.Connection, dia: str) -> None:
    conn.execute("INSERT OR IGNORE INTO daily_summary (sent_on) VALUES (?)", (dia,))
    conn.commit()


def previsao_por_atracao(conn: sqlite3.Connection, config: dict, park_name: str) -> list[tuple]:
    """Melhor e pior hora de cada atração da watchlist, pelo histórico coletado.

    Devolve [(atração, (hora, média) abertura, (hora, média) melhor,
    (hora, média) pico, leituras)] ordenado pelo pico: as de maior pico são as
    que valem rope drop.

    A média da abertura entra porque é a decisão das 7h. Só "melhor hora" tende
    a apontar o fim da noite em toda atração, o que não ajuda a montar a manhã.
    """
    offset = park_utc_offset_horas(config)
    park_cfg = config["parks"].get(park_name, {})
    rows = conn.execute(
        """
        SELECT ride, CAST(strftime('%H', ts) AS INTEGER) AS h, AVG(wait_time), COUNT(*)
        FROM wait_times
        WHERE park = ? AND is_open = 1 AND wait_time IS NOT NULL
        GROUP BY ride, h
        """,
        (park_name,),
    ).fetchall()

    por_atracao: dict[str, list[tuple[int, float, int]]] = {}
    for ride, hora_utc, media, n in rows:
        if get_threshold(park_cfg, ride) is None:  # fora da watchlist ou single rider
            continue
        hora = (hora_utc + offset) % 24
        if hora not in HORAS_PARQUE:
            continue
        por_atracao.setdefault(ride, []).append((hora, media, n))

    previsao = []
    for ride, serie in por_atracao.items():
        serie.sort()  # por hora
        primeiras = serie[:2]  # duas primeiras horas com dado = janela do rope drop
        leituras_abertura = sum(item[2] for item in primeiras)
        abertura = (
            primeiras[0][0],
            sum(item[1] * item[2] for item in primeiras) / leituras_abertura,
        )
        melhor = min(serie, key=lambda item: item[1])
        pico = max(serie, key=lambda item: item[1])
        previsao.append(
            (ride, abertura, melhor[:2], pico[:2], sum(item[2] for item in serie))
        )
    previsao.sort(key=lambda item: item[3][1], reverse=True)
    return previsao


def format_daily_summary(conn: sqlite3.Connection, config: dict, park_name: str) -> str:
    """Resumo da manhã: o que esperar de cada atração hoje, pelo histórico."""
    agora = now_park(config)
    dias = conn.execute(
        "SELECT COUNT(DISTINCT date(ts)) FROM wait_times WHERE park = ?", (park_name,)
    ).fetchone()[0]

    # "hoje é dia de X" só quando for verdade: /resumo aceita qualquer parque,
    # em qualquer data, e afirmar isso num dia de coleta seria mentira.
    if park_name in is_alert_day(config):
        titulo = f"☀️ <b>Bom dia!</b> Hoje é dia de <b>{notifier.esc(park_name)}</b>"
    else:
        titulo = f"📊 <b>{notifier.esc(park_name)}</b> — previsão pelo histórico"
    cabecalho = [
        titulo,
        f"📅 {agora.strftime('%d/%m')} · {dias} dia(s) de histórico coletado",
        "",
    ]
    rodape = ["", "Mande /status para a fila de agora.", "Powered by Queue-Times.com"]

    previsao = previsao_por_atracao(conn, config, park_name)
    if not previsao:
        return "\n".join(
            cabecalho
            + ["Ainda não tenho histórico deste parque para prever nada."]
            + rodape
        )

    linhas = ["Ordenado pelo pico — as de cima são as de atacar no rope drop:", ""]
    for ride, (h_ab, m_ab), (h_bom, m_bom), (h_pico, m_pico), leituras in previsao:
        linhas.append(f"🎢 <b>{notifier.esc(ride)}</b>")
        linhas.append(f"     abertura {h_ab:02d}h ~{m_ab:.0f} min · pico {h_pico:02d}h ~{m_pico:.0f} min")
        linhas.append(f"     melhor do dia {h_bom:02d}h ~{m_bom:.0f} min · n={leituras}")
    return "\n".join(cabecalho + linhas + rodape)


def maybe_send_daily_summary(conn: sqlite3.Connection, config: dict, park_ids: dict[str, int]) -> None:
    """Manda o resumo uma vez por dia, na janela depois da hora configurada."""
    cfg = config.get("daily_summary", {})
    if not cfg.get("enabled", False):
        return

    agora = now_park(config)
    dia = agora.date().isoformat()
    if resumo_enviado(conn, dia):
        return

    alvo = hhmm_em_minutos(cfg.get("hour", "07:00"))
    minutos_agora = agora.hour * 60 + agora.minute
    if not alvo <= minutos_agora < alvo + JANELA_RESUMO_MINUTOS:
        return

    do_dia = [p for p in is_alert_day(config) if p in park_ids]
    if not do_dia:
        if cfg.get("only_park_days", True):
            return  # sem parque hoje: resumo diário viraria spam até outubro
        texto = (
            f"☀️ <b>Bom dia!</b> Hoje não é dia de parque — só coletando histórico.\n"
            f"Mande <code>/resumo &lt;parque&gt;</code> para a previsão de qualquer um."
        )
    else:
        texto = format_daily_summary(conn, config, do_dia[0])

    if notifier.send(texto):
        marcar_resumo_enviado(conn, dia)
        log.info("Resumo diário enviado (%s)", dia)


# ---------------------------------------------------------------- comandos

HELP = (
    "🎢 <b>Monitor de filas</b>\n\n"
    "/status — fila atual da watchlist do parque de hoje\n"
    "/status &lt;parque&gt; — fila de um parque específico (ex.: <code>/status Epcot</code>)\n"
    "/menores — ranking das menores filas do parque inteiro agora\n"
    "/menores &lt;parque&gt; — ranking de um parque específico\n"
    "/resumo — previsão do dia pelo histórico (o mesmo das 7h)\n"
    "/resumo &lt;parque&gt; — previsão de um parque específico\n"
    "/parques — parques monitorados\n"
    "/perto — melhor atração agora considerando fila + caminhada\n"
    "/health — estado do monitor (coleta, banco, parques)\n"
    "/help — esta mensagem\n\n"
    "Os alertas automáticos continuam rodando sozinhos nos dias de parque.\n"
    "Powered by Queue-Times.com"
)


def format_health(conn: sqlite3.Connection, config: dict, park_ids: dict[str, int]) -> str:
    """Estado do monitor: dá para responder 'está vivo?' sem abrir SSH."""
    ultima = conn.execute("SELECT MAX(ts) FROM wait_times").fetchone()[0]
    total, dias = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT date(ts)) FROM wait_times"
    ).fetchone()
    esperados = len(config.get("parks", {}))
    tamanho_mb = DB_PATH.stat().st_size / 1_000_000 if DB_PATH.exists() else 0

    if ultima:
        atraso = (utc_now() - datetime.fromisoformat(ultima)).total_seconds() / 60
        # 2 ciclos de folga antes de chamar de atraso
        saude = "🟢" if atraso < POLL_INTERVAL_SECONDS / 60 * 2 else "🔴"
        coleta = f"há {atraso:.0f} min"
    else:
        saude, coleta = "🔴", "nunca"

    alertas = conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    do_dia = [p for p in is_alert_day(config) if p in park_ids]
    return "\n".join([
        f"{saude} <b>Monitor de filas</b>",
        "",
        f"Última coleta: {coleta}",
        f"Parques resolvidos: {len(park_ids)}/{esperados}",
        f"Histórico: {total:,} leituras em {dias} dia(s) · {tamanho_mb:.1f} MB".replace(",", "."),
        f"Alertas já enviados: {alertas}",
        f"Hoje: {notifier.esc(do_dia[0]) if do_dia else 'sem parque (modo coleta)'}",
        "",
        f"Ciclo a cada {POLL_INTERVAL_SECONDS // 60} min · {now_park(config).strftime('%Hh%M')} no parque",
    ])


PEDIR_LOCALIZACAO = (
    "📍 Manda sua localização que eu digo para onde ir.\n\n"
    "Toque no botão abaixo, ou no clipe 📎 → <b>Localização</b>.\n\n"
    "Eu comparo <b>fila + caminhada</b> das atrações da sua watchlist e devolvo "
    "as melhores, com rota."
)


def responder_localizacao(latitude: float, longitude: float, conn: sqlite3.Connection,
                          config: dict, park_ids: dict[str, int], coords: dict) -> str:
    """Resposta ao envio de localização: melhor atração por tempo total."""
    if not coords.get("parks"):
        return ("Ainda não tenho as coordenadas das atrações. "
                "Rode <code>python coords.py</code> uma vez no servidor.")

    posicao = (latitude, longitude)
    park_name = localizacao.parque_mais_proximo(posicao, coords)
    if park_name is None:
        return ("📍 Não achei nenhum parque monitorado perto de você. "
                "Este recurso só funciona dentro dos parques.")
    if park_name not in park_ids:
        return f"O parque mais próximo é {notifier.esc(park_name)}, mas ele não resolveu na API."

    try:
        payload = fetch_queue_times(park_ids[park_name])
    except requests.RequestException as exc:
        log.error("Falha ao buscar %s para localização: %s", park_name, exc)
        return "Não consegui falar com a API do Queue-Times agora. Tenta de novo em 1 min."
    troca = None
    park_to_park = localizacao.config_park_to_park(config)
    outro_parque = park_to_park.get("parks", {}).get(park_name)
    if park_to_park.get("enabled") and outro_parque in park_ids:
        try:
            payload_outro = fetch_queue_times(park_ids[outro_parque])
            troca = localizacao.avaliar_troca_park_to_park(
                posicao, park_name, payload, payload_outro, config, coords, conn)
        except requests.RequestException as exc:
            log.warning("Park-to-Park indisponível para %s: %s", outro_parque, exc)
    return localizacao.format_perto(
        posicao, park_name, payload, config, coords, conn, troca=troca)


def match_parks(query: str, park_ids: dict[str, int]) -> list[str]:
    """Parques cujo nome contém a busca (mesmo espírito do match da watchlist)."""
    q = query.strip().lower()
    return [name for name in park_ids if q in name.lower()]


def format_status(park_name: str, payload: dict, config: dict,
                  conn: sqlite3.Connection | None = None) -> str:
    """Monta a resposta do /status: watchlist do parque com a fila de agora."""
    park_cfg = config["parks"].get(park_name, {})
    limite_obsoleto = config.get("alert", {}).get("max_staleness_minutes", OBSOLETO_MINUTOS_PADRAO)
    abertas, fechadas, obsoletas = [], [], []
    for _land, ride in iter_rides(payload):
        threshold = get_threshold(park_cfg, ride["name"])
        if threshold is None:  # fora da watchlist: não polui a mensagem
            continue
        wait = ride.get("wait_time")
        if not ride.get("is_open") or wait is None:
            fechadas.append(ride["name"])
        elif leitura_obsoleta(ride, limite_obsoleto):
            obsoletas.append((ride["name"], wait))
        else:
            abertas.append((wait, ride["name"], threshold))

    if not abertas and not fechadas and not obsoletas:
        return (
            f"🎢 <b>{notifier.esc(park_name)}</b>\n\n"
            "Nenhuma atração da watchlist voltou da API agora. "
            "Se persistir, confira os nomes em <code>watchlist.json</code>."
        )

    agora = now_park(config).strftime("%Hh%M")
    linhas = [f"🎢 <b>{notifier.esc(park_name)}</b>", f"🕒 {agora} no horário do parque", ""]
    for wait, ride, threshold in sorted(abertas):
        marca = "✅" if wait <= threshold else "▫️"
        seta = marca_tendencia(conn, park_name, ride)
        linhas.append(f"{marca} {notifier.esc(ride)} — <b>{wait} min</b>{seta} (alerta ≤ {threshold})")
    for ride in sorted(fechadas):
        linhas.append(f"🔒 {notifier.esc(ride)} — fechada")
    for ride, wait in sorted(obsoletas):
        linhas.append(f"⏳ {notifier.esc(ride)} — {wait} min (dado desatualizado)")
    linhas += ["", "✅ = no ponto de ir · ↓ caindo · ↑ subindo (últimos 35 min)",
               "Powered by Queue-Times.com"]
    return "\n".join(linhas)


def handle_command(text: str, conn: sqlite3.Connection, config: dict, park_ids: dict[str, int]) -> str | None:
    """Interpreta um comando do chat. Devolve a resposta ou None (ignorar)."""
    # só a primeira linha: no Telegram dá para mandar vários comandos numa
    # mensagem só, e sem isso o resto vira argumento do primeiro.
    primeira_linha = text.strip().splitlines()[0] if text.strip() else ""
    parts = primeira_linha.split(maxsplit=1)
    if not parts:
        return None
    cmd = parts[0].split("@")[0].lower()  # em grupo o Telegram manda /status@NomeDoBot
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not cmd.startswith("/"):
        return None  # conversa solta no chat não é comando
    if cmd in ("/start", "/help", "/ajuda"):
        return HELP
    if cmd in ("/perto", "/agora"):
        return PEDIR_LOCALIZACAO
    if cmd == "/health":
        return format_health(conn, config, park_ids)
    if cmd == "/parques":
        nomes = "\n".join(f"• {notifier.esc(n)}" for n in park_ids)
        return f"🎢 <b>Parques monitorados</b>\n{nomes}"
    if cmd not in ("/status", "/resumo", "/menores"):
        return HELP

    if arg:
        matches = match_parks(arg, park_ids)
        if not matches:
            return f"Não achei parque com “{notifier.esc(arg)}”. Veja /parques."
        if len(matches) > 1:
            opcoes = "\n".join(f"• {notifier.esc(n)}" for n in matches)
            return f"“{notifier.esc(arg)}” casa com mais de um parque:\n{opcoes}"
        park_name = matches[0]
    else:
        do_dia = [p for p in is_alert_day(config) if p in park_ids]
        if not do_dia:
            return (
                "Hoje não é dia de parque — o monitor está só coletando histórico.\n"
                f"Use <code>{cmd} &lt;parque&gt;</code> para ver qualquer um. Veja /parques."
            )
        park_name = do_dia[0]

    if cmd == "/resumo":
        return format_daily_summary(conn, config, park_name)

    try:
        payload = fetch_queue_times(park_ids[park_name])
    except requests.RequestException as exc:
        log.error("Falha ao buscar %s para %s: %s", park_name, cmd, exc)
        return "Não consegui falar com a API do Queue-Times agora. Tenta de novo em 1 min."

    if cmd == "/menores":
        limite = config.get("top_alert", {}).get("list_size", 10)
        return format_menores(park_name, payload, config, limite)
    return format_status(park_name, payload, config, conn)


def serve_commands(offset: int | None, conn: sqlite3.Connection, config: dict,
                   park_ids: dict[str, int], timeout: int,
                   coords: dict | None = None) -> int | None:
    """Consome os updates pendentes e responde. Devolve o novo offset."""
    coords = coords if coords is not None else {"parks": {}, "rides": {}}
    for update in notifier.get_updates(offset, timeout=timeout):
        offset = update["update_id"] + 1
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = message.get("chat", {}).get("id")
        localizacao = message.get("location")
        text = message.get("text", "")
        if not text and not localizacao:
            continue
        if not notifier.is_authorized(chat_id):
            log.warning("Comando ignorado de chat não autorizado: %s", chat_id)
            continue

        if localizacao:
            log.info("Localização recebida")
            notifier.send(responder_localizacao(
                localizacao["latitude"], localizacao["longitude"],
                conn, config, park_ids, coords))
            continue

        resposta = handle_command(text, conn, config, park_ids)
        if resposta:
            log.info("Comando atendido: %s", text.split()[0])
            # /perto só é útil com o botão de localização junto
            botao = notifier.BOTAO_LOCALIZACAO if resposta is PEDIR_LOCALIZACAO else None
            notifier.send(resposta, botao)
    return offset


def wait_serving_commands(offset: int | None, conn: sqlite3.Connection, config: dict,
                          park_ids: dict[str, int], seconds: int,
                          coords: dict | None = None) -> int | None:
    """Espera até o próximo ciclo atendendo comandos. Nunca propaga exceção."""
    deadline = time.monotonic() + seconds
    while True:
        restante = deadline - time.monotonic()
        if restante <= 0:
            return offset
        if not notifier.configured():  # sem Telegram, só dorme e segue coletando
            time.sleep(min(restante, 30))
            continue
        espera = int(min(COMMAND_POLL_SECONDS, restante))
        if espera < 1:  # sobra menos de 1s: não vale abrir outro long poll
            time.sleep(restante)
            return offset
        try:
            offset = serve_commands(offset, conn, config, park_ids, espera, coords)
        except Exception:  # noqa: BLE001 — comando quebrado não derruba a coleta
            log.exception("Erro ao atender comandos do Telegram")
            time.sleep(min(restante, 5))


# ---------------------------------------------------------------- ciclo

def run_cycle(conn: sqlite3.Connection, config: dict, park_ids: dict[str, int]) -> dict[str, dict]:
    """Coleta, grava e alerta. Devolve os payloads do ciclo, reaproveitados pelo
    alerta de menores filas — assim ele não repete a chamada na API."""
    payloads: dict[str, dict] = {}
    ts = utc_now().isoformat()
    alert_parks = is_alert_day(config)
    cooldown = config.get("alert", {}).get("cooldown_minutes", 45)
    obsoleto_min = config.get("alert", {}).get("max_staleness_minutes", OBSOLETO_MINUTOS_PADRAO)
    quiet = in_quiet_hours(config)

    for park_name, park_id in park_ids.items():
        try:
            payload = fetch_queue_times(park_id)
        except requests.RequestException as exc:
            log.error("Falha ao buscar %s: %s", park_name, exc)
            continue
        payloads[park_name] = payload

        rows = []
        for land, ride in iter_rides(payload):
            rows.append(
                (ts, park_name, land, ride["name"], ride.get("wait_time"), int(ride.get("is_open", False)))
            )

            # alerta somente no parque do dia, fora do quiet hours
            if quiet or park_name not in alert_parks:
                continue
            if not ride.get("is_open"):
                continue
            if leitura_obsoleta(ride, obsoleto_min):
                continue
            threshold = get_threshold(config["parks"].get(park_name, {}), ride["name"])
            if threshold is None:
                continue
            wait = ride.get("wait_time")
            if wait is None or wait > threshold:
                continue
            if recently_alerted(conn, park_name, ride["name"], cooldown):
                continue
            seta = marca_tendencia(conn, park_name, ride["name"])
            if notifier.send(notifier.format_alert(park_name, ride["name"], wait, threshold, seta)):
                mark_alerted(conn, park_name, ride["name"])
                log.info("ALERTA: %s / %s = %s min", park_name, ride["name"], wait)

        if rows:
            conn.executemany(
                "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            log.info("%s: %d atrações gravadas", park_name, len(rows))

    return payloads


def main() -> None:
    config = load_config()
    problemas = validar_config(config)
    for problema in problemas:
        log.error("Config inválida: %s", problema)
    if problemas:
        raise SystemExit("watchlist.json com problema — corrija os erros acima")

    if not notifier.configured():
        log.warning(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes: coleta segue, "
            "mas nenhum alerta e nenhum comando vão funcionar"
        )

    conn = init_db()

    park_names = list(config["parks"].keys())
    park_ids = resolve_park_ids(park_names)
    log.info("Parques resolvidos: %s", park_ids)
    if not park_ids:
        raise SystemExit("Nenhum parque resolvido — verifique os nomes em watchlist.json")

    missing = set(park_names) - set(park_ids)
    if missing:
        log.warning("Parques NÃO monitorados (nome não bateu na API): %s", missing)

    # park_days usa os nomes de parks como chave; se divergirem, o dia de parque
    # vira dia de coleta sem nenhum erro aparecer — daí o aviso explícito.
    agendados = {p for dias in config.get("park_days", {}).values() for p in dias}
    orfaos = agendados - set(config["parks"])
    if orfaos:
        log.warning(
            "park_days aponta para parque que não existe em parks: %s — "
            "esses dias NÃO vão alertar",
            sorted(orfaos),
        )
    sem_id = agendados & missing
    if sem_id:
        log.warning("Dias de parque sem alerta (parque não resolvido na API): %s", sorted(sem_id))

    coords = localizacao.load_coords()
    com_coord = sum(len(r) for r in coords.get("rides", {}).values())
    if com_coord:
        log.info("coords.json: %d atrações com coordenada — /perto ativo", com_coord)
    else:
        log.info("Sem coords.json — /perto vai pedir para rodar coords.py")

    notifier.send(
        "✅ Monitor de filas iniciado. Mande /status para ver a fila agora.\n"
        "Powered by Queue-Times.com"
    )
    offset = notifier.drop_pending_updates()

    while True:
        payloads: dict[str, dict] = {}
        try:
            payloads = run_cycle(conn, config, park_ids)
        except Exception:  # noqa: BLE001 — loop nunca deve morrer
            log.exception("Erro no ciclo de coleta")
        try:
            maybe_send_top_alert(conn, config, park_ids, payloads)
        except Exception:  # noqa: BLE001 — alerta quebrado não pode parar a coleta
            log.exception("Erro no alerta de menores filas")
        try:
            maybe_send_daily_summary(conn, config, park_ids)
        except Exception:  # noqa: BLE001 — resumo quebrado não pode parar a coleta
            log.exception("Erro no resumo diário")
        offset = wait_serving_commands(offset, conn, config, park_ids,
                                       POLL_INTERVAL_SECONDS, coords)


if __name__ == "__main__":
    main()
