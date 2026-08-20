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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("monitor")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "history.db"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"

PARKS_URL = "https://queue-times.com/parks.json"
QUEUE_URL = "https://queue-times.com/parks/{park_id}/queue_times.json"

POLL_INTERVAL_SECONDS = 300  # 5 min — mesma frequência de atualização da API
COMMAND_POLL_SECONDS = 20    # long polling do Telegram dentro da espera entre ciclos
HTTP_TIMEOUT = 15


# ---------------------------------------------------------------- config

def load_config() -> dict:
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


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
    resp = requests.get(PARKS_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    groups = resp.json()

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


def fetch_queue_times(park_id: int) -> dict:
    resp = requests.get(QUEUE_URL.format(park_id=park_id), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


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


# ---------------------------------------------------------------- menores filas

def menores_filas(payload: dict, config: dict, park_name: str, limite: int,
                  apenas_watchlist: bool) -> list[tuple[int, str, int | None]]:
    """(espera, atração, threshold) das menores filas abertas, da menor para a maior.

    Filas paralelas ficam de fora: reportam 0 min sem dado e ocupariam o topo do
    ranking inteiro, que é justamente o que o comando existe para mostrar.
    """
    park_cfg = config["parks"].get(park_name, {})
    abertas = []
    for _land, ride in iter_rides(payload):
        nome = ride["name"]
        if fila_paralela(nome):
            continue
        if not ride.get("is_open"):
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


def format_top_alert(park_name: str, ranking: list, config: dict) -> str:
    """Mensagem curta do alerta recorrente — chega a cada N min, tem que ser enxuta."""
    agora = now_park(config).strftime("%Hh%M")
    medalhas = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
    linhas = [f"⚡ <b>Menores filas agora</b> · {notifier.esc(park_name)} · {agora}"]
    for i, (wait, ride, threshold) in enumerate(ranking):
        marca = " ✅" if threshold is not None and wait <= threshold else ""
        medalha = medalhas[i] if i < len(medalhas) else "•"
        linhas.append(f"{medalha} {notifier.esc(ride)} — <b>{wait} min</b>{marca}")
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
    if notifier.send(format_top_alert(park_name, ranking, config)):
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
    "/help — esta mensagem\n\n"
    "Os alertas automáticos continuam rodando sozinhos nos dias de parque.\n"
    "Powered by Queue-Times.com"
)


def match_parks(query: str, park_ids: dict[str, int]) -> list[str]:
    """Parques cujo nome contém a busca (mesmo espírito do match da watchlist)."""
    q = query.strip().lower()
    return [name for name in park_ids if q in name.lower()]


def format_status(park_name: str, payload: dict, config: dict) -> str:
    """Monta a resposta do /status: watchlist do parque com a fila de agora."""
    park_cfg = config["parks"].get(park_name, {})
    abertas, fechadas = [], []
    for _land, ride in iter_rides(payload):
        threshold = get_threshold(park_cfg, ride["name"])
        if threshold is None:  # fora da watchlist: não polui a mensagem
            continue
        wait = ride.get("wait_time")
        if ride.get("is_open") and wait is not None:
            abertas.append((wait, ride["name"], threshold))
        else:
            fechadas.append(ride["name"])

    if not abertas and not fechadas:
        return (
            f"🎢 <b>{notifier.esc(park_name)}</b>\n\n"
            "Nenhuma atração da watchlist voltou da API agora. "
            "Se persistir, confira os nomes em <code>watchlist.json</code>."
        )

    agora = now_park(config).strftime("%Hh%M")
    linhas = [f"🎢 <b>{notifier.esc(park_name)}</b>", f"🕒 {agora} no horário do parque", ""]
    for wait, ride, threshold in sorted(abertas):
        marca = "✅" if wait <= threshold else "▫️"
        linhas.append(f"{marca} {notifier.esc(ride)} — <b>{wait} min</b> (alerta ≤ {threshold})")
    for ride in sorted(fechadas):
        linhas.append(f"🔒 {notifier.esc(ride)} — fechada")
    linhas += ["", "✅ = já está no ponto de ir", "Powered by Queue-Times.com"]
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
    return format_status(park_name, payload, config)


def serve_commands(offset: int | None, conn: sqlite3.Connection, config: dict, park_ids: dict[str, int], timeout: int) -> int | None:
    """Consome os updates pendentes e responde. Devolve o novo offset."""
    for update in notifier.get_updates(offset, timeout=timeout):
        offset = update["update_id"] + 1
        message = update.get("message") or update.get("edited_message") or {}
        text = message.get("text", "")
        if not text:
            continue
        chat_id = message.get("chat", {}).get("id")
        if not notifier.is_authorized(chat_id):
            log.warning("Comando ignorado de chat não autorizado: %s", chat_id)
            continue
        resposta = handle_command(text, conn, config, park_ids)
        if resposta:
            log.info("Comando atendido: %s", text.split()[0])
            notifier.send(resposta)
    return offset


def wait_serving_commands(offset: int | None, conn: sqlite3.Connection, config: dict, park_ids: dict[str, int], seconds: int) -> int | None:
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
            offset = serve_commands(offset, conn, config, park_ids, timeout=espera)
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
            threshold = get_threshold(config["parks"].get(park_name, {}), ride["name"])
            if threshold is None:
                continue
            wait = ride.get("wait_time")
            if wait is None or wait > threshold:
                continue
            if recently_alerted(conn, park_name, ride["name"], cooldown):
                continue
            if notifier.send(notifier.format_alert(park_name, ride["name"], wait, threshold)):
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
        offset = wait_serving_commands(offset, conn, config, park_ids, POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
