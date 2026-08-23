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
import hmac
import logging
import os
import sqlite3
import time
import math
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import localizacao  # noqa: E402 — ciclo resolvido: uso só dentro de funções
import notifier
import personagens

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("monitor")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "history.db"
UPTIME_KUMA_PUSH_URL = os.environ.get("UPTIME_KUMA_PUSH_URL", "").strip()
APP_GIT_SHA = os.environ.get("APP_GIT_SHA", "unknown").strip() or "unknown"
FAMILY_ACCESS_PASSWORD = os.environ.get("FAMILY_ACCESS_PASSWORD", "")
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
    # Lembrete sem id nunca seria marcado como enviado e repetiria a cada ciclo
    # dentro da janela; id repetido faria o segundo nunca sair.
    vistos = set()
    for i, lembrete in enumerate(config.get("reminders", [])):
        onde = f"reminders[{i}]"
        lembrete_id = lembrete.get("id")
        if not lembrete_id:
            problemas.append(f"{onde} sem 'id' — sem ele o lembrete repetiria a cada ciclo")
        elif lembrete_id in vistos:
            problemas.append(f"{onde}: id {lembrete_id!r} repetido")
        else:
            vistos.add(lembrete_id)
        try:
            date.fromisoformat(lembrete.get("date", ""))
        except ValueError:
            problemas.append(f"{onde}.date = {lembrete.get('date')!r} não é uma data ISO")
        hora = lembrete.get("hour")
        if hora is not None and not re.fullmatch(r"\d{2}:\d{2}", hora):
            problemas.append(f"{onde}.hour = {hora!r} não está em HH:MM")
        if not lembrete.get("text"):
            problemas.append(f"{onde} sem 'text' — a mensagem chegaria vazia")
    return problemas


def utc_now() -> datetime:
    """Agora em UTC, sem tzinfo.

    datetime.utcnow() está deprecado no 3.12, mas o .replace(tzinfo=None) é de
    propósito: o banco já tem histórico gravado sem offset e misturar os dois
    formatos na mesma coluna bagunçaria o strftime do analyze.py.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- db

def aplicar_pragmas(conn: sqlite3.Connection) -> None:
    """WAL e espera por lock: dois processos dividem este banco.

    Sem WAL o container da API e o do monitor se bloqueiam — a manutenção
    diária apaga em lote e, durante isso, o site responderia 503. O
    busy_timeout dá 5s de paciência em vez de estourar na hora.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")


def conectar_somente_leitura() -> sqlite3.Connection:
    """Conexão de leitura para quem não é o monitor (a API do site).

    Só o monitor cria e escreve no banco; abrir em modo `ro` garante isso no
    nível do SQLite, em vez de confiar que o código não vai escrever.
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    aplicar_pragmas(conn)
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS top_alert_park ("
        "park TEXT PRIMARY KEY, sent_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_summary_park ("
        "sent_on TEXT NOT NULL, park TEXT NOT NULL, PRIMARY KEY (sent_on, park))"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS route_rejections (
            ts              TEXT NOT NULL,
            park            TEXT NOT NULL,
            ride            TEXT NOT NULL,
            direct_meters   REAL NOT NULL,
            route_meters    INTEGER NOT NULL,
            route_minutes   INTEGER NOT NULL,
            reason          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ride_watches (
            park       TEXT NOT NULL,
            ride       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (park, ride)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reopen_alerts (
            park    TEXT NOT NULL,
            ride    TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_location (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            latitude   REAL NOT NULL,
            longitude  REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reopen_park_ride_ts "
        "ON reopen_alerts (park, ride, sent_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS database_maintenance ("
        "ran_on TEXT PRIMARY KEY, deleted_rows INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS authorized_chats ("
        "chat_id TEXT PRIMARY KEY, authorized_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_locations ("
        "chat_id TEXT PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ride_watch_subscriptions ("
        "chat_id TEXT NOT NULL, park TEXT NOT NULL, ride TEXT NOT NULL, "
        "created_at TEXT NOT NULL, PRIMARY KEY (chat_id, park, ride))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS character_alert_preferences ("
        "chat_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, "
        "radius_meters INTEGER NOT NULL DEFAULT 500, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS character_alerts ("
        "chat_id TEXT NOT NULL, park TEXT NOT NULL, character_name TEXT NOT NULL, "
        "sent_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_alerts_recent "
        "ON character_alerts (chat_id, park, character_name, sent_at)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS character_last_checks ("
        "chat_id TEXT PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL, "
        "checked_at TEXT NOT NULL)"
    )
    # Só as tentativas ERRADAS de /entrar: acerto limpa a conta do chat, então
    # guardar sucesso aqui seria uma linha inserida e apagada no mesmo passo.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS auth_attempts ("
        "chat_id TEXT NOT NULL, attempted_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_attempts_chat_ts "
        "ON auth_attempts (chat_id, attempted_at)"
    )
    # Quem já foi avisado que o acesso é restrito não precisa ser avisado de novo:
    # responder a cada mensagem de estranho é ruído e confirma que o bot existe.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS unauthorized_notices ("
        "chat_id TEXT PRIMARY KEY, notified_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reminders_sent ("
        "id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)"
    )
    if notifier.CHAT_ID:
        principal = str(notifier.CHAT_ID)
        conn.execute(
            "INSERT OR IGNORE INTO authorized_chats (chat_id, authorized_at) VALUES (?, ?)",
            (principal, utc_now().isoformat()),
        )
        antiga = conn.execute(
            "SELECT latitude, longitude, updated_at FROM user_location WHERE id = 1"
        ).fetchone()
        if antiga:
            conn.execute(
                "INSERT OR IGNORE INTO user_locations "
                "(chat_id, latitude, longitude, updated_at) VALUES (?, ?, ?, ?)",
                (principal, *antiga),
            )
        conn.execute(
            "INSERT OR IGNORE INTO ride_watch_subscriptions "
            "(chat_id, park, ride, created_at) "
            "SELECT ?, park, ride, created_at FROM ride_watches",
            (principal,),
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
    """POST JSON preservando retry, timeout e tratamento de status centralizados."""
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
FRACAO_PARQUE_OPERANDO = 0.25


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


def estado_parque_payload(payload: dict, limite_min: int = OBSOLETO_MINUTOS_PADRAO) -> str:
    """`operando`, `fechado` ou `desconhecido` sem depender de horários externos.

    O feed noturno costuma deixar todas as atrações fechadas. A fração aberta
    separa isso de uma quebra individual; leituras majoritariamente obsoletas
    indicam feed parado e nunca podem produzir alertas de fechamento/reabertura.
    """
    rides = [r for _land, r in iter_rides(payload) if not fila_paralela(r["name"])]
    if not rides:
        return "desconhecido"
    recentes = [r for r in rides if not leitura_obsoleta(r, limite_min)]
    if len(recentes) < len(rides) * 0.5:
        return "desconhecido"
    fracao = sum(bool(r.get("is_open")) for r in recentes) / len(recentes)
    return "operando" if fracao >= FRACAO_PARQUE_OPERANDO else "fechado"


def parque_operava_no_ultimo_ciclo(conn: sqlite3.Connection, park: str) -> bool:
    row = conn.execute("SELECT MAX(ts) FROM wait_times WHERE park = ?", (park,)).fetchone()
    if not row or not row[0]:
        return False
    abertas, total = conn.execute(
        "SELECT SUM(is_open), COUNT(*) FROM wait_times WHERE park = ? AND ts = ?",
        (park, row[0]),
    ).fetchone()
    return bool(total and abertas / total >= FRACAO_PARQUE_OPERANDO)


def fila_paralela(ride_name: str) -> bool:
    """True para single rider / fila virtual — entrada separada na API, tempo 0."""
    return any(termo in ride_name.lower() for termo in FILAS_IGNORADAS)


_SO_ALFANUMERICO = re.compile(r"[^a-z0-9]+")


def normalizar_nome_api(nome: str) -> str:
    """Nome comparável: só letras e números, minúsculo, espaço simples.

    O match por pedaço comparava os nomes crus e a atração sumia inteira quando
    a pontuação divergia — sem alerta, fora do /status, do /perto e do resumo, e
    sem nenhum erro no log, porque nome não casado é indistinguível de atração
    fora da watchlist. Casos reais colhidos da API em 23/08/2026:

        Mario Kart™: Bowser's Challenge     ™ no meio, antes dos dois-pontos
        Buzz Lightyear’s Space Ranger Spin  apóstrofo curvo, não o reto
        Rock ’n’ Roller Coaster Starring…   idem, dois deles
        TRANSFORMERS™ The Ride-3D           ™, sem dois-pontos, hífen no 3D

    NFD e não NFKD de propósito: a decomposição de compatibilidade transforma
    "™" em "TM" e grudaria as letras no nome ("transformerstm").
    """
    texto = unicodedata.normalize("NFD", nome.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return _SO_ALFANUMERICO.sub(" ", texto).strip()


def nome_watchlist(park_cfg: dict, ride_name: str) -> str | None:
    """Nome canônico da watchlist correspondente ao nome devolvido pela API."""
    if fila_paralela(ride_name):
        return None
    alvo = normalizar_nome_api(ride_name)
    for watched in park_cfg.get("attractions", {}):
        procurado = normalizar_nome_api(watched)
        if procurado in alvo or alvo in procurado:
            return watched
    return None


def get_threshold(park_cfg: dict, ride_name: str) -> int | None:
    """Threshold da atração; None se não estiver na watchlist do parque."""
    nome = nome_watchlist(park_cfg, ride_name)
    return park_cfg.get("attractions", {}).get(nome) if nome else None


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


def maiores_filas(payload: dict, config: dict, limite: int) -> list[tuple[int, str]]:
    """Maiores filas abertas e atuais, ignorando filas paralelas sem dado confiável."""
    limite_obsoleto = config.get("alert", {}).get(
        "max_staleness_minutes", OBSOLETO_MINUTOS_PADRAO
    )
    abertas = []
    for _land, ride in iter_rides(payload):
        nome = ride["name"]
        wait = ride.get("wait_time")
        if (fila_paralela(nome) or not ride.get("is_open") or wait is None
                or leitura_obsoleta(ride, limite_obsoleto)):
            continue
        abertas.append((wait, nome))
    abertas.sort(key=lambda item: (-item[0], item[1]))
    return abertas[:limite]


def format_ranking_atual(rankings: list[tuple[int, str, str]], config: dict) -> str:
    """Ranking atual de um ou vários parques."""
    if not rankings:
        return "🏆 <b>Maiores filas agora</b>\n\nNenhuma atração aberta com dado atual."
    agora = now_park(config).strftime("%Hh%M")
    linhas = ["🏆 <b>Maiores filas agora</b>", f"🕒 {agora} no horário do parque", ""]
    for posicao, (wait, ride, park) in enumerate(rankings, 1):
        linhas.append(
            f"<b>{posicao}. {notifier.esc(ride)}</b> — {wait} min\n"
            f"     {notifier.esc(park)}"
        )
    linhas += ["", "Ranking por tempo de espera, não por número de visitantes.",
               "Powered by Queue-Times.com"]
    return "\n".join(linhas)


def periodo_ranking(config: dict, dias: int) -> tuple[str, str]:
    """Limites UTC-naive para hoje ou últimos N dias no fuso do parque."""
    agora = now_park(config)
    inicio_local = datetime.combine(
        agora.date() - timedelta(days=dias - 1), datetime.min.time(), tzinfo=agora.tzinfo
    )
    inicio = inicio_local.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    fim = agora.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    return inicio, fim


def ranking_historico(conn: sqlite3.Connection, config: dict, dias: int,
                      limite: int = 10) -> list[tuple[float, int, int, str, str]]:
    """Atrações mais concorridas por média, com pico e tamanho da amostra."""
    inicio, fim = periodo_ranking(config, dias)
    rows = conn.execute(
        """
        SELECT AVG(wait_time), MAX(wait_time), COUNT(*), ride, park
        FROM wait_times
        WHERE ts >= ? AND ts <= ? AND is_open = 1 AND wait_time IS NOT NULL
        GROUP BY park, ride
        """,
        (inicio, fim),
    ).fetchall()
    validas = [row for row in rows if not fila_paralela(row[3])]
    validas.sort(key=lambda row: (-row[0], -row[1], row[3], row[4]))
    return validas[:limite]


def format_ranking_historico(conn: sqlite3.Connection, config: dict, dias: int) -> str:
    """Formata concorrência estimada pelo histórico de filas."""
    ranking = ranking_historico(conn, config, dias)
    periodo = "hoje" if dias == 1 else "últimos 7 dias"
    linhas = [f"📊 <b>Atrações mais concorridas — {periodo}</b>", ""]
    if not ranking:
        return "\n".join(linhas + ["Ainda não há leituras suficientes nesse período."])
    for posicao, (media, pico, amostras, ride, park) in enumerate(ranking, 1):
        linhas.append(
            f"<b>{posicao}. {notifier.esc(ride)}</b> — média {media:.0f} min · pico {pico} min\n"
            f"     {notifier.esc(park)} · {amostras} leituras"
        )
    linhas += ["", "Estimativa de concorrência pelo tempo de fila; não mede visitantes."]
    return "\n".join(linhas)


# A classificacao e deliberadamente conservadora: so entra no /chuva o que tem
# experiencia principal interna. Ausencia na lista significa "nao confirmado",
# nunca uma suposicao de que a atracao seja coberta.
ATRACOES_SEGURAS_CHUVA = {
    "Space Mountain", "Peter Pan's Flight", "Haunted Mansion",
    "Pirates of the Caribbean", "Buzz Lightyear's Space Ranger Spin",
    "Guardians of the Galaxy: Cosmic Rewind", "Frozen Ever After",
    "Remy's Ratatouille Adventure", "Soarin' Around the World", "Mission: SPACE",
    "Star Wars: Rise of the Resistance", "Millennium Falcon: Smugglers Run",
    "Mickey & Minnie's Runaway Railway", "Tower of Terror",
    "Rock 'n' Roller Coaster", "Toy Story Mania!", "Avatar Flight of Passage",
    "Na'vi River Journey", "Harry Potter and the Escape from Gringotts",
    "Revenge of the Mummy", "Transformers: The Ride 3D",
    "MEN IN BLACK Alien Attack", "The Simpsons Ride", "E.T. Adventure",
    "Villain-Con Minion Blast", "Harry Potter and the Forbidden Journey",
    "The Amazing Adventures of Spider-Man", "Skull Island: Reign of Kong",
    "Harry Potter and the Battle at the Ministry",
    "Monsters Unchained: The Frankenstein Experiment",
    "Mario Kart: Bowser's Challenge",
}


def resolver_atracao(config: dict, query: str) -> list[tuple[str, str]]:
    """Resolve trecho do nome apenas na watchlist, sem escolher ambiguidades."""
    q = query.strip().lower()
    if not q:
        return []
    return [
        (park, ride)
        for park, park_cfg in config.get("parks", {}).items()
        for ride in park_cfg.get("attractions", {})
        if q in ride.lower()
    ]


def _format_opcoes_atracao(matches: list[tuple[str, str]]) -> str:
    return "\n".join(
        f"• {notifier.esc(ride)} — {notifier.esc(park)}" for park, ride in matches[:12]
    )


def guardar_localizacao(conn: sqlite3.Connection, latitude: float, longitude: float,
                        chat_id=None) -> None:
    chat_id = str(chat_id if chat_id is not None else notifier.CHAT_ID)
    conn.execute(
        "INSERT OR REPLACE INTO user_locations "
        "(chat_id, latitude, longitude, updated_at) VALUES (?, ?, ?, ?)",
        (chat_id, latitude, longitude, utc_now().isoformat()),
    )
    conn.commit()


def ultima_localizacao(conn: sqlite3.Connection, max_minutos: int = 180,
                      chat_id=None) -> tuple[float, float] | None:
    chat_id = str(chat_id if chat_id is not None else notifier.CHAT_ID)
    row = conn.execute(
        "SELECT latitude, longitude, updated_at FROM user_locations WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if not row:
        return None
    try:
        idade = utc_now() - datetime.fromisoformat(row[2])
    except (TypeError, ValueError):
        return None
    return (row[0], row[1]) if idade <= timedelta(minutes=max_minutos) else None


def vigiar_atracao(conn: sqlite3.Connection, park: str, ride: str, chat_id=None) -> str:
    chat_id = str(chat_id if chat_id is not None else notifier.CHAT_ID)
    conn.execute(
        "INSERT OR REPLACE INTO ride_watch_subscriptions "
        "(chat_id, park, ride, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, park, ride, utc_now().isoformat()),
    )
    conn.commit()
    return (
        f"👀 Vou vigiar <b>{notifier.esc(ride)}</b> — {notifier.esc(park)}.\n"
        "Avisarei uma vez quando houver transição confirmada de fechada para aberta."
    )


def cancelar_vigia(conn: sqlite3.Connection, park: str, ride: str, chat_id=None) -> str:
    chat_id = str(chat_id if chat_id is not None else notifier.CHAT_ID)
    removidas = conn.execute(
        "DELETE FROM ride_watch_subscriptions WHERE chat_id = ? AND park = ? AND ride = ?",
        (chat_id, park, ride),
    ).rowcount
    conn.commit()
    return (f"✅ Vigilância removida: {notifier.esc(ride)}."
            if removidas else f"Não havia vigilância ativa para {notifier.esc(ride)}.")


def format_vigias(conn: sqlite3.Connection, chat_id=None) -> str:
    chat_id = str(chat_id if chat_id is not None else notifier.CHAT_ID)
    rows = conn.execute(
        "SELECT park, ride FROM ride_watch_subscriptions "
        "WHERE chat_id = ? ORDER BY park, ride", (chat_id,)
    ).fetchall()
    if not rows:
        return ("👀 Nenhuma atração sendo vigiada.\n"
                "Use <code>/vigiar &lt;atração&gt;</code>.")
    linhas = ["👀 <b>Alertas de reabertura ativos</b>", ""]
    linhas += [f"• {notifier.esc(ride)} — {notifier.esc(park)}" for park, ride in rows]
    linhas += ["", "Para remover: <code>/vigiar cancelar &lt;atração&gt;</code>"]
    return "\n".join(linhas)


def estado_anterior(conn: sqlite3.Connection, park: str, ride: str) -> int | None:
    row = conn.execute(
        "SELECT is_open FROM wait_times WHERE park = ? AND ride = ? ORDER BY ts DESC LIMIT 1",
        (park, ride),
    ).fetchone()
    return row[0] if row else None


def reabertura_em_cooldown(conn: sqlite3.Connection, park: str, ride: str,
                           minutos: int) -> bool:
    corte = (utc_now() - timedelta(minutes=minutos)).isoformat()
    return conn.execute(
        "SELECT 1 FROM reopen_alerts WHERE park = ? AND ride = ? AND sent_at >= ? LIMIT 1",
        (park, ride, corte),
    ).fetchone() is not None


def maybe_alertar_reabertura(conn: sqlite3.Connection, config: dict, park: str,
                             ride: dict, anterior: int | None,
                             parque_operava: bool, estado_atual: str) -> None:
    """Alerta apenas em transição observada 0→1; reinício sem histórico não dispara."""
    if (not parque_operava or estado_atual != "operando" or anterior != 0
            or not ride.get("is_open") or leitura_obsoleta(ride)):
        return
    nome = ride["name"]
    park_cfg = config.get("parks", {}).get(park, {})
    canonico = nome_watchlist(park_cfg, nome)
    assinantes = []
    if canonico:
        assinantes = [row[0] for row in conn.execute(
            "SELECT chat_id FROM ride_watch_subscriptions WHERE park = ? AND ride = ?",
            (park, canonico),
        ).fetchall()]
    cfg = config.get("reopen_alert", {})
    automatico = (cfg.get("enabled", True) and park in is_alert_day(config)
                  and canonico is not None and not in_quiet_hours(config))
    if not assinantes and not automatico:
        return
    cooldown = int(cfg.get("cooldown_minutes", 90))
    if reabertura_em_cooldown(conn, park, nome, cooldown):
        return
    wait = ride.get("wait_time")
    fila = f"Fila publicada: <b>{wait} min</b>." if wait is not None else "Fila ainda sem estimativa."
    texto = (f"🚨 <b>REABRIU</b>\n\n<b>{notifier.esc(nome)}</b>\n"
             f"{notifier.esc(park)}\n{fila}\n\n"
             "Transição confirmada pelo Queue-Times; confira a entrada antes de caminhar.")
    destinatarios = set(assinantes)
    if automatico and notifier.CHAT_ID:
        destinatarios.add(str(notifier.CHAT_ID))
    enviados = [destino for destino in destinatarios
                if notifier.send(texto, chat_id=destino)]
    if enviados:
        conn.execute(
            "INSERT INTO reopen_alerts (park, ride, sent_at) VALUES (?, ?, ?)",
            (park, nome, utc_now().isoformat()),
        )
        if assinantes:
            conn.execute(
                "DELETE FROM ride_watch_subscriptions WHERE park = ? AND ride = ?",
                (park, canonico),
            )
        conn.commit()


def historico_estado_hoje(conn: sqlite3.Connection, config: dict, park: str,
                          ride: str) -> tuple[int, int | None]:
    """(quantidade de fechamentos, minutos da interrupcao atual)."""
    inicio, _fim = periodo_ranking(config, 1)
    todos = conn.execute(
        "SELECT ts, ride, is_open FROM wait_times WHERE park = ? AND ts >= ? ORDER BY ts",
        (park, inicio),
    ).fetchall()
    por_ciclo = {}
    for ts, nome, aberto in todos:
        ciclo = por_ciclo.setdefault(ts, {"abertas": 0, "total": 0, "ride": None})
        ciclo["abertas"] += bool(aberto)
        ciclo["total"] += 1
        if nome == ride:
            ciclo["ride"] = bool(aberto)
    rows = [(ts, dados["ride"]) for ts, dados in por_ciclo.items()
            if dados["ride"] is not None
            and dados["abertas"] / dados["total"] >= FRACAO_PARQUE_OPERANDO]
    fechamentos = sum(1 for i, (_ts, aberto) in enumerate(rows)
                      if not aberto and i > 0 and rows[i - 1][1])
    inicio_fechado = None
    for ts, aberto in reversed(rows):
        if aberto:
            break
        inicio_fechado = ts
    minutos = None
    if inicio_fechado:
        minutos = max(0, round((utc_now() - datetime.fromisoformat(inicio_fechado)).total_seconds() / 60))
    return fechamentos, minutos


def format_fechadas(conn: sqlite3.Connection, config: dict, park: str, payload: dict) -> str:
    limite = config.get("alert", {}).get("max_staleness_minutes", OBSOLETO_MINUTOS_PADRAO)
    estado = estado_parque_payload(payload, limite)
    if estado == "fechado":
        return (f"🌙 <b>{notifier.esc(park)}</b> parece estar fechado ou ainda abrindo.\n\n"
                "Não vou tratar o fechamento geral como quebra de atrações.")
    if estado == "desconhecido":
        return (f"⚠️ <b>{notifier.esc(park)}</b> com feed insuficiente ou desatualizado.\n\n"
                "Estado desconhecido: nenhuma quebra será afirmada agora.")
    fechadas = []
    obsoletas = 0
    for _land, ride in iter_rides(payload):
        if fila_paralela(ride["name"]) or ride.get("is_open"):
            continue
        if leitura_obsoleta(ride, limite):
            obsoletas += 1
            continue
        fechamentos, minutos = historico_estado_hoje(conn, config, park, ride["name"])
        fechadas.append((minutos if minutos is not None else -1, fechamentos, ride["name"]))
    if not fechadas:
        return f"🟢 <b>{notifier.esc(park)}</b>\n\nNenhuma atração aparece como fechada agora."
    fechadas.sort(key=lambda item: (-item[0], item[2]))
    linhas = [f"🔧 <b>Atrações fechadas — {notifier.esc(park)}</b>", ""]
    for minutos, vezes, ride in fechadas:
        duracao = f"há ~{minutos} min" if minutos >= 0 else "detectada agora"
        linhas.append(f"🔴 <b>{notifier.esc(ride)}</b> — {duracao} · {vezes} fechamento(s) hoje")
    linhas += ["", "Use <code>/vigiar &lt;atração&gt;</code> para receber a reabertura.",
               "Estado atualizado a cada ~5 min · Powered by Queue-Times.com"]
    if obsoletas:
        linhas.insert(-2, f"⚠️ {obsoletas} estado(s) antigo(s) omitido(s).")
    return "\n".join(linhas)


def _ride_atual(payload: dict, park_cfg: dict, canonico: str) -> dict | None:
    for _land, ride in iter_rides(payload):
        if nome_watchlist(park_cfg, ride["name"]) == canonico:
            return ride
    return None


def format_confianca(conn: sqlite3.Connection, config: dict, park: str,
                     canonico: str, payload: dict) -> str:
    ride = _ride_atual(payload, config["parks"][park], canonico)
    if not ride:
        return "A atração não apareceu na resposta atual da API."
    if not ride.get("is_open") or ride.get("wait_time") is None:
        return f"🔒 <b>{notifier.esc(ride['name'])}</b> está fechada ou sem fila publicada."
    perfil = localizacao.perfil_historico(
        conn, config, park, ride["name"], int(ride["wait_time"])
    )
    titulo = (f"🎯 <b>Confiança da fila</b>\n\n<b>{notifier.esc(ride['name'])}</b> — "
              f"{ride['wait_time']} min\n{notifier.esc(park)}")
    if not perfil:
        return titulo + "\n\nAinda não há 12 leituras comparáveis neste dia da semana e horário."
    classe = localizacao.classificar_fila(int(ride["wait_time"]), perfil)
    amplitude = perfil["p75"] - perfil["p25"]
    confianca = "alta" if amplitude <= 15 else "média" if amplitude <= 30 else "baixa"
    return (titulo + f"\n\n{classe}\nConfiança histórica: <b>{confianca}</b>\n"
            f"Faixa comum: {perfil['p25']:.0f}–{perfil['p75']:.0f} min · "
            f"mediana {perfil['mediana']:.0f} · n={perfil['n']}\n\n"
            "Compara o valor publicado com o mesmo dia da semana e horário; não mede a espera real.")


def format_lotacao(conn: sqlite3.Connection, config: dict, park: str, payload: dict) -> str:
    atuais = [int(r["wait_time"]) for _l, r in iter_rides(payload)
              if r.get("is_open") and r.get("wait_time") is not None
              and not fila_paralela(r["name"]) and not leitura_obsoleta(r)]
    fechadas = sum(1 for _l, r in iter_rides(payload)
                   if not r.get("is_open") and not fila_paralela(r["name"]))
    if not atuais:
        return f"⚠️ <b>{notifier.esc(park)}</b> sem filas atuais suficientes para estimar lotação."
    momento = now_park(config)
    fuso = ZoneInfo(config["trip"]["timezone"])
    def selecionar_historico(rows):
        selecionado = []
        for ts, wait in rows:
            try:
                d = datetime.fromisoformat(ts)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                d = d.astimezone(fuso)
            except (TypeError, ValueError):
                continue
            if d.weekday() == momento.weekday() and d.hour == momento.hour:
                selecionado.append(int(wait))
        return selecionado

    corte = (momento.astimezone(timezone.utc).replace(tzinfo=None)
             - timedelta(days=56)).isoformat()
    base_sql = ("SELECT ts, wait_time FROM wait_times WHERE park = ? AND is_open = 1 "
                "AND wait_time IS NOT NULL")
    historico = selecionar_historico(
        conn.execute(base_sql + " AND ts >= ?", (park, corte)).fetchall()
    )
    if len(historico) < 12:
        historico = selecionar_historico(conn.execute(base_sql, (park,)).fetchall())
    media = sum(atuais) / len(atuais)
    if len(historico) < 12:
        nivel = "dados históricos insuficientes"
        detalhe = f"Média publicada agora: {media:.0f} min"
    else:
        p25 = localizacao.percentil(historico, .25)
        mediana = localizacao.percentil(historico, .5)
        p75 = localizacao.percentil(historico, .75)
        nivel = ("🟢 leve" if media <= p25 else "🟡 normal" if media <= mediana
                 else "🟠 cheia" if media <= p75 else "🔴 excepcionalmente cheia")
        detalhe = f"Agora {media:.0f} min · faixa comum {p25:.0f}–{p75:.0f} min · n={len(historico)}"
    instavel = "⚠️ operação instável" if fechadas >= 3 else "operação estável"
    return (f"👥 <b>Lotação estimada — {notifier.esc(park)}</b>\n\n"
            f"Nível: <b>{nivel}</b>\n{detalhe}\n"
            f"Atrações fechadas: {fechadas} · {instavel}\n\n"
            "Estimativa pela distribuição das filas; não é contagem de pessoas.")


def _format_itens_proximos(titulo: str, itens: list, park: str, limite: int = 3) -> str:
    if not itens:
        return f"{titulo}\n\nNenhuma opção elegível com dado atual."
    linhas = [titulo, f"📍 {notifier.esc(park)}", ""]
    medalhas = ("🥇", "🥈", "🥉")
    for i, item in enumerate(itens[:limite]):
        total, fila, caminhada, metros, nome = item[:5]
        if total is None:
            linhas.append(f"{medalhas[i]} {notifier.esc(nome)} — fila {fila} min · sem rota")
        else:
            linhas.append(f"{medalhas[i]} <b>{notifier.esc(nome)}</b> — {total} min total\n"
                          f"     fila {fila} + caminhada {caminhada} min ({metros:.0f} m)")
    return "\n".join(linhas)


def format_chuva(conn: sqlite3.Connection, config: dict, park: str, payload: dict,
                  coords: dict) -> str:
    posicao = ultima_localizacao(conn)
    if posicao and coords:
        itens = localizacao._ranking_detalhado(posicao, park, payload, config, coords, conn)
        seguros = [i for i in itens if nome_watchlist(config["parks"][park], i[4])
                   in ATRACOES_SEGURAS_CHUVA]
        return (_format_itens_proximos("☔ <b>Melhores opções internas na chuva</b>", seguros, park)
                + "\n\nLista conservadora: atrações externas ou não confirmadas ficam de fora.")
    seguros = []
    for _land, ride in iter_rides(payload):
        canonico = nome_watchlist(config["parks"][park], ride["name"])
        if (canonico in ATRACOES_SEGURAS_CHUVA and ride.get("is_open")
                and ride.get("wait_time") is not None and not leitura_obsoleta(ride)):
            seguros.append((ride["wait_time"], ride["name"]))
    seguros.sort()
    linhas = [f"☔ <b>Opções internas — {notifier.esc(park)}</b>", ""]
    linhas += [f"• {notifier.esc(nome)} — {wait} min" for wait, nome in seguros[:8]]
    linhas += ["", "Envie sua localização para incluir caminhada."]
    return "\n".join(linhas)


def format_plano(conn: sqlite3.Connection, config: dict, park: str, payload: dict,
                 coords: dict) -> str:
    posicao = ultima_localizacao(conn)
    if not posicao:
        return ("📍 Preciso de uma localização recente para montar o plano. "
                "Envie sua localização e depois use /plano.")
    if not coords.get("rides", {}).get(park):
        return "Não há coordenadas suficientes deste parque para montar o plano."
    escolhidos, usados = [], set()
    atual = posicao
    for _ in range(3):
        itens = localizacao._ranking_detalhado(atual, park, payload, config, coords, conn)
        elegiveis = [i for i in itens if i[4] not in usados]
        if not elegiveis:
            break
        item = elegiveis[0]
        escolhidos.append(item)
        usados.add(item[4])
        if item[5] is not None:
            atual = item[5]
    return (_format_itens_proximos("🗺️ <b>Plano dinâmico — próximas 3 atrações</b>",
                                  escolhidos, park)
            + "\n\nRecalculado etapa a etapa com as filas atuais; confirme novamente após cada atração.")


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


def top_alert_atrasado(conn: sqlite3.Connection, intervalo_min: int,
                       park: str | None = None) -> bool:
    """True se já passou o intervalo desde o último envio (ou se nunca houve)."""
    row = (conn.execute("SELECT sent_at FROM top_alert_park WHERE park = ?", (park,)).fetchone()
           if park else conn.execute("SELECT sent_at FROM top_alert WHERE id = 1").fetchone())
    if not row:
        return True
    return datetime.fromisoformat(row[0]) <= utc_now() - timedelta(minutes=intervalo_min)


def marcar_top_alert(conn: sqlite3.Connection, park: str | None = None) -> None:
    if park:
        conn.execute("INSERT OR REPLACE INTO top_alert_park (park, sent_at) VALUES (?, ?)",
                     (park, utc_now().isoformat()))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO top_alert (id, sent_at) VALUES (1, ?)",
            (utc_now().isoformat(),),
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
    parques_alvo = do_dia or list(park_ids)
    por_parque = len(parques_alvo) > 1
    for park_name in parques_alvo:
        payload = payloads.get(park_name)
        if payload is None or not top_alert_atrasado(
                conn, cfg.get("every_minutes", 10), park_name if por_parque else None):
            continue
        ranking = menores_filas(
            payload, config, park_name, cfg.get("count", 3), apenas_watchlist=True
        )
        if ranking and notifier.send(format_top_alert(park_name, ranking, config, conn)):
            if por_parque:
                marcar_top_alert(conn, park_name)
            marcar_top_alert(conn)  # compatibilidade/telemetria global
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


def resumo_enviado(conn: sqlite3.Connection, dia: str, park: str | None = None) -> bool:
    if park:
        return conn.execute(
            "SELECT 1 FROM daily_summary_park WHERE sent_on = ? AND park = ? LIMIT 1",
            (dia, park),
        ).fetchone() is not None
    return conn.execute("SELECT 1 FROM daily_summary WHERE sent_on = ? LIMIT 1",
                        (dia,)).fetchone() is not None


def marcar_resumo_enviado(conn: sqlite3.Connection, dia: str, park: str | None = None) -> None:
    if park:
        conn.execute("INSERT OR IGNORE INTO daily_summary_park (sent_on, park) VALUES (?, ?)",
                     (dia, park))
    else:
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

    alvo = hhmm_em_minutos(cfg.get("hour", "07:00"))
    minutos_agora = agora.hour * 60 + agora.minute
    if not alvo <= minutos_agora < alvo + JANELA_RESUMO_MINUTOS:
        return

    do_dia = [p for p in is_alert_day(config) if p in park_ids]
    if not do_dia:
        if cfg.get("only_park_days", True):
            return  # sem parque hoje: resumo diário viraria spam até outubro
        if resumo_enviado(conn, dia):
            return
        texto = (
            f"☀️ <b>Bom dia!</b> Hoje não é dia de parque — só coletando histórico.\n"
            f"Mande <code>/resumo &lt;parque&gt;</code> para a previsão de qualquer um."
        )
        if notifier.send(texto):
            marcar_resumo_enviado(conn, dia)
            log.info("Resumo diário enviado (%s)", dia)
        return

    for park in do_dia:
        if resumo_enviado(conn, dia, park):
            continue
        texto = format_daily_summary(conn, config, park)
        if notifier.send(texto):
            marcar_resumo_enviado(conn, dia, park)
            marcar_resumo_enviado(conn, dia)  # compatibilidade/telemetria global
            log.info("Resumo diário enviado (%s / %s)", dia, park)


# ---------------------------------------------------------------- lembretes

def lembrete_enviado(conn: sqlite3.Connection, lembrete_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM reminders_sent WHERE id = ? LIMIT 1", (lembrete_id,)
    ).fetchone() is not None


def marcar_lembrete_enviado(conn: sqlite3.Connection, lembrete_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO reminders_sent (id, sent_at) VALUES (?, ?)",
        (lembrete_id, utc_now().isoformat()),
    )
    conn.commit()


def format_lembretes(config: dict) -> str:
    """Lembretes que ainda vão acontecer, para conferir sem esperar o dia."""
    hoje = now_park(config).date()
    futuros = []
    for lembrete in config.get("reminders", []):
        try:
            data = date.fromisoformat(lembrete["date"])
        except (KeyError, ValueError):
            continue
        if data >= hoje:
            futuros.append((data, lembrete))
    if not futuros:
        return "⏰ <b>Lembretes</b>\n\nNenhum lembrete pendente."
    futuros.sort(key=lambda item: (item[0], item[1].get("hour", "07:00")))
    linhas = ["⏰ <b>Lembretes pendentes</b>", ""]
    for data, lembrete in futuros:
        faltam = (data - hoje).days
        quando = "hoje" if faltam == 0 else ("amanhã" if faltam == 1 else f"em {faltam} dias")
        linhas.append(
            f"📅 <b>{data.strftime('%d/%m')}</b> {lembrete.get('hour', '07:00')} · {quando}\n"
            f"     {notifier.esc(lembrete.get('text', ''))}"
        )
    return "\n".join(linhas)


def maybe_send_reminders(conn: sqlite3.Connection, config: dict) -> None:
    """Prazos com data marcada — Lightning Lane, conferências de véspera.

    O monitor sabe a fila, mas quem perde a janela das 7h para comprar o
    Multi-Pass paga em fila o dia inteiro. Só depende do relógio: nenhuma
    chamada de API, nenhum parque envolvido, funciona em dia de coleta também.
    """
    agora = now_park(config)
    hoje = agora.date().isoformat()
    minutos_agora = agora.hour * 60 + agora.minute
    for lembrete in config.get("reminders", []):
        if lembrete.get("date") != hoje:
            continue
        # A chave é o id do config, não a posição na lista: reordenar ou
        # acrescentar lembrete não pode fazer o já enviado sair de novo.
        lembrete_id = lembrete.get("id")
        if not lembrete_id or lembrete_enviado(conn, lembrete_id):
            continue
        alvo = hhmm_em_minutos(lembrete.get("hour", "07:00"))
        if not alvo <= minutos_agora < alvo + JANELA_RESUMO_MINUTOS:
            continue
        texto = (f"⏰ <b>Lembrete — {agora.strftime('%d/%m')}</b>\n\n"
                 f"{notifier.esc(lembrete.get('text', ''))}")
        if notifier.send(texto):
            marcar_lembrete_enviado(conn, lembrete_id)
            log.info("Lembrete enviado: %s", lembrete_id)


def enviar_teste_alertas(conn: sqlite3.Connection, config: dict, park_name: str,
                         payload: dict, chat_id=None) -> None:
    """Envia os três formatos reais sem registrar cooldown ou resumo enviado."""
    ranking = menores_filas(payload, config, park_name, 3, apenas_watchlist=True)
    if not ranking:
        notifier.send(
            f"🧪 <b>TESTE</b> — nenhuma atração aberta da watchlist em "
            f"{notifier.esc(park_name)}", chat_id=chat_id
        )
        return

    wait, ride, threshold = ranking[0]
    mensagens = (
        "🧪 <b>TESTE — alerta de threshold</b>\n\n"
        + notifier.format_alert(park_name, ride, wait, threshold or wait),
        "🧪 <b>TESTE — Top-3 menores</b>\n\n"
        + format_top_alert(park_name, ranking, config, conn),
        "🧪 <b>TESTE — resumo das 7h</b>\n\n"
        + format_daily_summary(conn, config, park_name),
    )
    for mensagem in mensagens:
        notifier.send(mensagem, chat_id=chat_id)
    log.info("Teste explícito dos três alertas executado (%s)", park_name)


# ---------------------------------------------------------------- comandos

def chat_autorizado(conn: sqlite3.Connection, chat_id) -> bool:
    if chat_id is None:
        return False
    if notifier.is_authorized(chat_id):
        return True
    return conn.execute(
        "SELECT 1 FROM authorized_chats WHERE chat_id = ?", (str(chat_id),)
    ).fetchone() is not None


# O bot é alcançável por qualquer pessoa que descubra o nome dele. Sem freio, a
# senha familiar é chutável na velocidade que o Telegram aceitar.
ENTRAR_TENTATIVAS_MAX = 5
ENTRAR_JANELA_MINUTOS = 60
# Responder a cada mensagem de estranho confirma o bot e vira ruído: avisa uma vez.
AVISO_RESTRITO_HORAS = 24


def tentativas_entrar_recentes(conn: sqlite3.Connection, chat_id,
                               janela_min: int = ENTRAR_JANELA_MINUTOS) -> int:
    corte = (utc_now() - timedelta(minutes=janela_min)).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM auth_attempts "
        "WHERE chat_id = ? AND attempted_at >= ?",
        (str(chat_id), corte),
    ).fetchone()[0]


def registrar_erro_entrar(conn: sqlite3.Connection, chat_id) -> None:
    conn.execute(
        "INSERT INTO auth_attempts (chat_id, attempted_at) VALUES (?, ?)",
        (str(chat_id), utc_now().isoformat()),
    )
    conn.commit()


def entrar_bloqueado(conn: sqlite3.Connection, chat_id) -> bool:
    return tentativas_entrar_recentes(conn, chat_id) >= ENTRAR_TENTATIVAS_MAX


def autenticar_familiar(conn: sqlite3.Connection, chat_id, senha: str) -> str:
    if not FAMILY_ACCESS_PASSWORD:
        return "O acesso familiar ainda não foi configurado pelo administrador."
    if entrar_bloqueado(conn, chat_id):
        log.warning("Chat %s bloqueado por excesso de tentativas de /entrar", chat_id)
        return f"🚫 Muitas tentativas. Tente de novo em {ENTRAR_JANELA_MINUTOS} minutos."
    if not hmac.compare_digest(senha, FAMILY_ACCESS_PASSWORD):
        registrar_erro_entrar(conn, chat_id)
        restantes = max(0, ENTRAR_TENTATIVAS_MAX - tentativas_entrar_recentes(conn, chat_id))
        log.warning("Senha familiar incorreta do chat %s (%d tentativa(s) restante(s))",
                    chat_id, restantes)
        return f"Senha familiar incorreta. Tentativas restantes: {restantes}."
    # Acerto zera o histórico: o freio existe contra quem chuta, não contra quem
    # errou de dedo antes de acertar.
    conn.execute("DELETE FROM auth_attempts WHERE chat_id = ?", (str(chat_id),))
    conn.execute(
        "INSERT OR REPLACE INTO authorized_chats (chat_id, authorized_at) VALUES (?, ?)",
        (str(chat_id), utc_now().isoformat()),
    )
    conn.commit()
    log.info("Acesso familiar liberado para o chat %s", chat_id)
    return "✅ Acesso familiar liberado. Use /help para ver os comandos."


def revogar_acesso(conn: sqlite3.Connection, alvo, solicitante) -> str:
    """Tira um chat da lista. O chat principal do .env não pode ser revogado."""
    alvo = str(alvo)
    if notifier.CHAT_ID and alvo == str(notifier.CHAT_ID):
        return ("O chat principal vem do <code>.env</code> e não pode ser revogado "
                "por comando.")
    apagados = conn.execute(
        "DELETE FROM authorized_chats WHERE chat_id = ?", (alvo,)
    ).rowcount
    conn.commit()
    if not apagados:
        return f"O chat <code>{notifier.esc(alvo)}</code> não estava liberado."
    log.info("Acesso revogado do chat %s (pedido por %s)", alvo, solicitante)
    return f"🔒 Acesso revogado do chat <code>{notifier.esc(alvo)}</code>."


def deve_avisar_nao_autorizado(conn: sqlite3.Connection, chat_id) -> bool:
    """True só no primeiro contato (ou depois de 24h) — evita virar eco de spam."""
    corte = (utc_now() - timedelta(hours=AVISO_RESTRITO_HORAS)).isoformat()
    recente = conn.execute(
        "SELECT 1 FROM unauthorized_notices WHERE chat_id = ? AND notified_at >= ?",
        (str(chat_id), corte),
    ).fetchone()
    if recente:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO unauthorized_notices (chat_id, notified_at) VALUES (?, ?)",
        (str(chat_id), utc_now().isoformat()),
    )
    conn.commit()
    return True

HELP = (
    "🎢 <b>Monitor de filas</b>\n\n"
    "/status — fila atual da watchlist do parque de hoje\n"
    "/status &lt;parque&gt; — fila de um parque específico (ex.: <code>/status Epcot</code>)\n"
    "/menores — ranking das menores filas do parque inteiro agora\n"
    "/menores &lt;parque&gt; — ranking de um parque específico\n"
    "/ranking — maiores filas agora em todos os parques\n"
    "/ranking &lt;parque&gt; — maiores filas agora em um parque\n"
    "/ranking hoje — atrações mais concorridas hoje pelo histórico\n"
    "/ranking semana — atrações mais concorridas nos últimos 7 dias\n"
    "/fechadas &lt;parque&gt; — quebras atuais, duração e instabilidade\n"
    "/vigiar &lt;atração&gt; — avisa uma vez quando a atração reabrir\n"
    "/confianca &lt;atração&gt; — compara a fila com o histórico equivalente\n"
    "/lotacao &lt;parque&gt; — pressão estimada pelas filas e fechamentos\n"
    "/plano — próximas três atrações por fila + caminhada\n"
    "/chuva — opções internas por fila + caminhada\n"
    "/resumo — previsão do dia pelo histórico (o mesmo das 7h)\n"
    "/resumo &lt;parque&gt; — previsão de um parque específico\n"
    "/parques — parques monitorados\n"
    "/perto — melhor atração agora considerando fila + caminhada\n"
    "/personagens_perto — encontros abertos perto da sua localização\n"
    "/alerta_personagens on|off — liga ou desliga avisos por proximidade\n"
    "/lembretes — prazos que ainda vão chegar (Lightning Lane, conferências)\n"
    "/health — estado do monitor (coleta, banco, parques)\n"
    "/teste_alertas &lt;parque&gt; — envia os três alertas com prefixo de teste\n"
    "/teste_park_to_park — simula no Telegram uma recomendação entre parques\n"
    "/entrar &lt;senha&gt; — libera este chat para uso familiar\n"
    "/sair — remove este chat da lista de liberados\n"
    "/revogar &lt;chat_id&gt; — só no chat principal: tira o acesso de outro chat\n"
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
    rotas_rejeitadas = conn.execute(
        "SELECT COUNT(*) FROM route_rejections").fetchone()[0]
    do_dia = [p for p in is_alert_day(config) if p in park_ids]
    return "\n".join([
        f"{saude} <b>Monitor de filas</b>",
        "",
        f"Última coleta: {coleta}",
        f"Parques resolvidos: {len(park_ids)}/{esperados}",
        f"Histórico: {total:,} leituras em {dias} dia(s) · {tamanho_mb:.1f} MB".replace(",", "."),
        f"Alertas já enviados: {alertas}",
        f"Rotas implausíveis descartadas: {rotas_rejeitadas}",
        f"Versão: <code>{notifier.esc(APP_GIT_SHA)}</code>",
        f"Hoje: {', '.join(notifier.esc(p) for p in do_dia) if do_dia else 'sem parque (modo coleta)'}",
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
    if not coords.get("rides"):
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
    cfg_p2p = localizacao.config_park_to_park(config)
    outro = cfg_p2p.get("parks", {}).get(park_name) if cfg_p2p.get("enabled") else None
    if outro in park_ids:
        try:
            payload_outro = fetch_queue_times(park_ids[outro])
            troca = localizacao.avaliar_troca_park_to_park(
                posicao, park_name, payload, payload_outro, config, coords, conn)
        except requests.RequestException as exc:
            log.warning("Park-to-Park indisponível para %s: %s", outro, exc)
    return localizacao.format_perto(
        posicao, park_name, payload, config, coords, conn, troca=troca)


def configurar_alerta_personagens(conn: sqlite3.Connection, chat_id, enabled: bool,
                                  raio: int = personagens.RAIO_PADRAO_METROS) -> str:
    raio = max(200, min(1000, int(raio)))
    conn.execute(
        "INSERT OR REPLACE INTO character_alert_preferences "
        "(chat_id, enabled, radius_meters, updated_at) VALUES (?, ?, ?, ?)",
        (str(chat_id), int(enabled), raio, utc_now().isoformat()),
    )
    conn.commit()
    if enabled:
        return (f"✅ Alertas de personagens ativados em um raio aproximado de {raio} m. "
                "Envie sua localização normal ou ao vivo.")
    return "🔕 Alertas automáticos de personagens desativados."


def preferencia_alerta_personagens(conn: sqlite3.Connection, chat_id) -> tuple[bool, int]:
    row = conn.execute(
        "SELECT enabled, radius_meters FROM character_alert_preferences WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    return (bool(row[0]), int(row[1])) if row else (True, personagens.RAIO_PADRAO_METROS)


def buscar_personagens_proximos(latitude: float, longitude: float, conn: sqlite3.Connection,
                                park_ids: dict[str, int], coords: dict,
                                raio: int = personagens.RAIO_PADRAO_METROS) -> tuple[str | None, list[dict]]:
    position = (latitude, longitude)
    park = localizacao.parque_mais_proximo(position, coords)
    if park is None or park not in park_ids:
        return park, []
    payload = fetch_queue_times(park_ids[park])
    return park, personagens.proximos(position, park, payload, coords, raio)


def format_personagens_proximos(park: str | None, items: list[dict], raio: int) -> str:
    if park is None:
        return "📍 Não achei um parque monitorado perto da sua última localização."
    if not items:
        return (f"🧑‍🎤 Nenhum encontro aberto e mapeado em até {raio} m agora.\n"
                "Os pontos são aproximados; confirme horários no aplicativo oficial.")
    lines = [f"🧑‍🎤 <b>Personagens perto de você</b> — {notifier.esc(park)}", ""]
    for item in items[:8]:
        wait = f" · fila {item['wait']} min" if item["wait"] is not None else ""
        url = personagens.maps_url(item["name"], park)
        lines.append(
            f"• <b>{notifier.esc(item['name'])}</b> — ~{item['meters']} m "
            f"(~{item['walk']} min){wait}\n  <a href=\"{url}\">Abrir no Google Maps</a>"
        )
    lines += ["", "Local do encontro aproximado; disponibilidade pela Queue-Times.com."]
    return "\n".join(lines)


def _alerta_personagem_recente(conn: sqlite3.Connection, chat_id, park: str,
                               name: str) -> bool:
    since = (utc_now() - timedelta(minutes=personagens.COOLDOWN_MINUTOS)).isoformat()
    return conn.execute(
        "SELECT 1 FROM character_alerts WHERE chat_id = ? AND park = ? "
        "AND character_name = ? AND sent_at >= ? LIMIT 1",
        (str(chat_id), park, name, since),
    ).fetchone() is not None


def enviar_alertas_personagens(latitude: float, longitude: float, conn: sqlite3.Connection,
                               park_ids: dict[str, int], coords: dict, chat_id) -> int:
    enabled, raio = preferencia_alerta_personagens(conn, chat_id)
    if not enabled:
        return 0
    previous = conn.execute(
        "SELECT latitude, longitude, checked_at FROM character_last_checks WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    if previous:
        try:
            age = utc_now() - datetime.fromisoformat(previous[2])
        except (TypeError, ValueError):
            age = timedelta.max
        moved = personagens.distancia_metros((previous[0], previous[1]), (latitude, longitude))
        if moved < 75 and age < timedelta(minutes=2):
            return 0
    conn.execute(
        "INSERT OR REPLACE INTO character_last_checks "
        "(chat_id, latitude, longitude, checked_at) VALUES (?, ?, ?, ?)",
        (str(chat_id), latitude, longitude, utc_now().isoformat()),
    )
    conn.commit()
    try:
        park, items = buscar_personagens_proximos(
            latitude, longitude, conn, park_ids, coords, raio)
    except requests.RequestException as exc:
        log.warning("Busca de personagens indisponível: %s", exc)
        return 0
    if not park:
        return 0
    sent = 0
    for item in items[:3]:
        if _alerta_personagem_recente(conn, chat_id, park, item["name"]):
            continue
        wait = f"\n⏱ Fila informada: <b>{item['wait']} min</b>" if item["wait"] is not None else ""
        text = (
            f"📍 <b>Personagem próximo</b>\n"
            f"🧑‍🎤 <b>{notifier.esc(item['name'])}</b>\n"
            f"🚶 Aproximadamente {item['meters']} m · {item['walk']} min{wait}\n"
            "Ponto oficial aproximado; confirme a disponibilidade no app do parque."
        )
        markup = {"inline_keyboard": [[
            {"text": "🗺 Google Maps", "url": personagens.maps_url(item["name"], park)}
        ]]}
        if notifier.send(text, markup, chat_id=chat_id):
            conn.execute(
                "INSERT INTO character_alerts (chat_id, park, character_name, sent_at) "
                "VALUES (?, ?, ?, ?)",
                (str(chat_id), park, item["name"], utc_now().isoformat()),
            )
            sent += 1
    if sent:
        conn.commit()
    return sent


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


def handle_command(text: str, conn: sqlite3.Connection, config: dict,
                   park_ids: dict[str, int], coords: dict | None = None,
                   chat_id=None) -> str | None:
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
    if cmd == "/alerta_personagens":
        option = arg.lower()
        if option in ("on", "ligar", "ativar"):
            return configurar_alerta_personagens(conn, chat_id, True)
        if option in ("off", "desligar", "desativar"):
            return configurar_alerta_personagens(conn, chat_id, False)
        enabled, raio = preferencia_alerta_personagens(conn, chat_id)
        state = "ativados" if enabled else "desativados"
        return f"Alertas de personagens: <b>{state}</b> · raio aproximado {raio} m."
    if cmd == "/personagens_perto":
        position = ultima_localizacao(conn, chat_id=chat_id)
        if position is None:
            return ("📍 Envie sua localização primeiro pelo botão do Telegram e tente "
                    "<code>/personagens_perto</code> novamente.")
        _enabled, raio = preferencia_alerta_personagens(conn, chat_id)
        try:
            park, items = buscar_personagens_proximos(
                position[0], position[1], conn, park_ids, coords or {}, raio)
        except requests.RequestException:
            return "Não consegui consultar os personagens agora. Tente novamente em 1 min."
        return format_personagens_proximos(park, items, raio)
    if cmd == "/sair":
        return revogar_acesso(conn, chat_id, chat_id)
    if cmd == "/revogar":
        if not notifier.is_authorized(chat_id):
            return "Só o chat principal pode revogar o acesso de outro chat."
        if not arg:
            liberados = conn.execute(
                "SELECT chat_id FROM authorized_chats ORDER BY authorized_at"
            ).fetchall()
            lista = "\n".join(f"• <code>{notifier.esc(c[0])}</code>" for c in liberados)
            return (f"Chats liberados:\n{lista}\n\n"
                    "Use <code>/revogar &lt;chat_id&gt;</code>.")
        return revogar_acesso(conn, arg, chat_id)
    if cmd == "/lembretes":
        return format_lembretes(config)
    if cmd == "/health":
        return format_health(conn, config, park_ids)
    if cmd == "/teste_park_to_park":
        exemplo = {
            "park": "Islands Of Adventure At Universal Orlando",
            "ride": "Harry Potter and the Forbidden Journey",
            "total": 42,
            "walk_to_station": 6,
            "train_wait": 8,
            "train_ride": 4,
            "walk_to_ride": 4,
            "ride_wait": 16,
            "savings": 21,
        }
        linhas = [
            "🧪 <b>SIMULAÇÃO Park-to-Park</b>",
            "Exemplo controlado — não usa as filas atuais e não altera o monitor.",
            "",
            *localizacao.format_troca_park_to_park(exemplo),
        ]
        return "\n".join(linhas)
    if cmd == "/parques":
        nomes = "\n".join(f"• {notifier.esc(n)}" for n in park_ids)
        return f"🎢 <b>Parques monitorados</b>\n{nomes}"
    if cmd == "/vigiar":
        if not arg:
            return format_vigias(conn, chat_id)
        cancelar = arg.lower().startswith("cancelar ")
        busca = arg.split(maxsplit=1)[1] if cancelar else arg
        matches = resolver_atracao(config, busca)
        if not matches:
            return f"Não achei atração com “{notifier.esc(busca)}”."
        if len(matches) > 1:
            return (f"“{notifier.esc(busca)}” é ambíguo:\n"
                    + _format_opcoes_atracao(matches))
        park, ride = matches[0]
        return (cancelar_vigia(conn, park, ride, chat_id) if cancelar
                else vigiar_atracao(conn, park, ride, chat_id))

    if cmd == "/confianca":
        if not arg:
            return "Use <code>/confianca &lt;atração&gt;</code>."
        matches = resolver_atracao(config, arg)
        if not matches:
            return f"Não achei atração com “{notifier.esc(arg)}”."
        if len(matches) > 1:
            return f"“{notifier.esc(arg)}” é ambíguo:\n" + _format_opcoes_atracao(matches)
        park, ride = matches[0]
        try:
            payload = fetch_queue_times(park_ids[park])
        except requests.RequestException as exc:
            log.error("Falha ao buscar %s para /confianca: %s", park, exc)
            return "Não consegui falar com a API do Queue-Times agora. Tenta de novo em 1 min."
        return format_confianca(conn, config, park, ride, payload)

    comandos_parque = ("/status", "/resumo", "/menores", "/ranking", "/teste_alertas",
                       "/fechadas", "/lotacao", "/plano", "/chuva")
    if cmd not in comandos_parque:
        return HELP
    if cmd == "/teste_alertas" and not arg:
        return "Use <code>/teste_alertas &lt;parque&gt;</code>. Veja /parques."

    if cmd == "/ranking" and arg.lower() in ("hoje", "semana"):
        return format_ranking_historico(conn, config, 1 if arg.lower() == "hoje" else 7)

    if cmd == "/ranking" and not arg:
        ranking = []
        falhas = 0
        for park_name, park_id in park_ids.items():
            try:
                payload = fetch_queue_times(park_id)
            except requests.RequestException as exc:
                falhas += 1
                log.error("Falha ao buscar %s para /ranking: %s", park_name, exc)
                continue
            ranking.extend((wait, ride, park_name)
                           for wait, ride in maiores_filas(payload, config, 10))
        ranking.sort(key=lambda item: (-item[0], item[1], item[2]))
        if falhas == len(park_ids):
            return "Não consegui falar com a API do Queue-Times agora. Tenta de novo em 1 min."
        return format_ranking_atual(ranking[:10], config)

    # Plano/chuva usam o parque realmente detectado quando há GPS recente.
    if cmd in ("/plano", "/chuva") and not arg and coords:
        posicao = ultima_localizacao(conn, chat_id=chat_id)
        detectado = localizacao.parque_mais_proximo(posicao, coords) if posicao else None
        if detectado in park_ids:
            arg = detectado

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
        if len(do_dia) > 1:
            opcoes = "\n".join(f"• {notifier.esc(p)}" for p in do_dia)
            return ("Há mais de um parque programado hoje. Especifique no comando:\n"
                    + opcoes)
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
    if cmd == "/teste_alertas":
        enviar_teste_alertas(conn, config, park_name, payload, chat_id)
        return None
    if cmd == "/ranking":
        ranking = [(wait, ride, park_name)
                   for wait, ride in maiores_filas(payload, config, 10)]
        return format_ranking_atual(ranking, config)
    if cmd == "/fechadas":
        return format_fechadas(conn, config, park_name, payload)
    if cmd == "/lotacao":
        return format_lotacao(conn, config, park_name, payload)
    if cmd == "/plano":
        return format_plano(conn, config, park_name, payload, coords or {})
    if cmd == "/chuva":
        return format_chuva(conn, config, park_name, payload, coords or {})
    return format_status(park_name, payload, config, conn)


def serve_commands(offset: int | None, conn: sqlite3.Connection, config: dict,
                   park_ids: dict[str, int], timeout: int,
                   coords: dict | None = None) -> int | None:
    """Consome os updates pendentes e responde. Devolve o novo offset."""
    coords = coords if coords is not None else {"parks": {}, "rides": {}}
    for update in notifier.get_updates(offset, timeout=timeout):
        offset = update["update_id"] + 1
        edited_location = "edited_message" in update
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = message.get("chat", {}).get("id")
        location_data = message.get("location")
        text = message.get("text", "")
        if not text and not location_data:
            continue
        primeira = text.strip().split(maxsplit=1) if text else []
        comando = primeira[0].split("@")[0].lower() if primeira else ""
        if comando == "/entrar":
            senha = primeira[1].strip() if len(primeira) > 1 else ""
            notifier.send(autenticar_familiar(conn, chat_id, senha), chat_id=chat_id)
            continue
        if not chat_autorizado(conn, chat_id):
            log.warning("Comando ignorado de chat não autorizado: %s", chat_id)
            if deve_avisar_nao_autorizado(conn, chat_id):
                notifier.send(
                    "🔒 Acesso restrito à família. Use <code>/entrar SUA_SENHA</code>.",
                    chat_id=chat_id,
                )
            continue

        if location_data:
            log.info("Localização %srecebida", "ao vivo atualizada " if edited_location else "")
            guardar_localizacao(
                conn, location_data["latitude"], location_data["longitude"], chat_id)
            if not edited_location:
                notifier.send(responder_localizacao(
                    location_data["latitude"], location_data["longitude"],
                    conn, config, park_ids, coords), chat_id=chat_id)
            enviar_alertas_personagens(
                location_data["latitude"], location_data["longitude"],
                conn, park_ids, coords, chat_id)
            continue

        resposta = handle_command(text, conn, config, park_ids, coords, chat_id)
        if resposta:
            log.info("Comando atendido: %s", text.split()[0])
            # /perto só é útil com o botão de localização junto
            botao = notifier.BOTAO_LOCALIZACAO if resposta is PEDIR_LOCALIZACAO else None
            notifier.send(resposta, botao, chat_id=chat_id)
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
        parque_operava = parque_operava_no_ultimo_ciclo(conn, park_name)
        estado_atual = estado_parque_payload(payload, obsoleto_min)
        ultimo_ts = conn.execute(
            "SELECT MAX(ts) FROM wait_times WHERE park = ?", (park_name,)
        ).fetchone()[0]
        estados_anteriores = dict(conn.execute(
            "SELECT ride, is_open FROM wait_times WHERE park = ? AND ts = ?",
            (park_name, ultimo_ts),
        ).fetchall()) if ultimo_ts else {}
        for land, ride in iter_rides(payload):
            anterior = estados_anteriores.get(ride["name"])
            rows.append(
                (ts, park_name, land, ride["name"], ride.get("wait_time"), int(ride.get("is_open", False)))
            )
            maybe_alertar_reabertura(
                conn, config, park_name, ride, anterior, parque_operava, estado_atual
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


def enviar_heartbeat(payloads: dict[str, dict], park_ids: dict[str, int]) -> None:
    """Confirma ao Uptime Kuma somente um ciclo completo e persistido.

    URL ausente desativa o recurso. Qualquer falha fica no log e nunca pode
    interromper coleta, alertas ou comandos.
    """
    if not UPTIME_KUMA_PUSH_URL or len(payloads) != len(park_ids):
        return
    try:
        resposta = requests.get(
            UPTIME_KUMA_PUSH_URL,
            params={"status": "up", "msg": f"ciclo completo: {len(payloads)} parques"},
            timeout=10,
        )
        resposta.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — heartbeat nunca derruba o monitor
        # Não inclua a exceção inteira: ela pode repetir a URL com o token Push.
        log.warning("Falha ao enviar heartbeat ao Uptime Kuma (%s)", type(exc).__name__)


def maybe_maintain_db(conn: sqlite3.Connection, config: dict) -> None:
    """Manutenção diária curta; histórico bruto só expira depois da viagem."""
    hoje = utc_now().date().isoformat()
    if conn.execute("SELECT 1 FROM database_maintenance WHERE ran_on = ?", (hoje,)).fetchone():
        return
    apagadas = 0
    corte_logs = (utc_now() - timedelta(days=90)).isoformat()
    for tabela, coluna in (("reopen_alerts", "sent_at"), ("route_rejections", "ts"),
                           ("alerts_sent", "sent_at"), ("auth_attempts", "attempted_at"),
                           ("unauthorized_notices", "notified_at")):
        apagadas += conn.execute(
            f"DELETE FROM {tabela} WHERE {coluna} < ?", (corte_logs,)
        ).rowcount
    retencao = max(30, int(config.get("database", {}).get("raw_retention_days", 180)))
    try:
        fim_viagem = date.fromisoformat(config["trip"]["end"])
    except (KeyError, ValueError):
        fim_viagem = date.max
    if utc_now().date() > fim_viagem + timedelta(days=30):
        corte_raw = (utc_now() - timedelta(days=retencao)).isoformat()
        apagadas += conn.execute("DELETE FROM wait_times WHERE ts < ?", (corte_raw,)).rowcount
    conn.execute("INSERT INTO database_maintenance (ran_on, deleted_rows) VALUES (?, ?)",
                 (hoje, apagadas))
    conn.execute("PRAGMA optimize")
    conn.commit()


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
        enviar_heartbeat(payloads, park_ids)
        try:
            maybe_send_top_alert(conn, config, park_ids, payloads)
        except Exception:  # noqa: BLE001 — alerta quebrado não pode parar a coleta
            log.exception("Erro no alerta de menores filas")
        try:
            maybe_send_daily_summary(conn, config, park_ids)
        except Exception:  # noqa: BLE001 — resumo quebrado não pode parar a coleta
            log.exception("Erro no resumo diário")
        try:
            maybe_send_reminders(conn, config)
        except Exception:  # noqa: BLE001 — lembrete quebrado não para a coleta
            log.exception("Erro no envio de lembretes")
        try:
            maybe_maintain_db(conn, config)
        except Exception:  # noqa: BLE001 — manutenção nunca derruba o monitor
            log.exception("Erro na manutenção do SQLite")
        offset = wait_serving_commands(offset, conn, config, park_ids,
                                       POLL_INTERVAL_SECONDS, coords)


if __name__ == "__main__":
    main()
