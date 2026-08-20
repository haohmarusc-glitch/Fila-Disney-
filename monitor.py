"""
Monitor de filas Disney/Universal Orlando.
Fonte de dados: Queue-Times.com (Powered by Queue-Times.com — https://queue-times.com)

Modos de operação (automáticos, por data):
- COLETA: fora das datas da viagem, apenas grava histórico no SQLite.
- ALERTA: nos dias de parque (park_days), além de gravar, envia alertas
  no Telegram quando uma atração da watchlist cai abaixo do threshold.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
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
HTTP_TIMEOUT = 15


# ---------------------------------------------------------------- config

def load_config() -> dict:
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


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
        matches = [pid for pname, pid in available.items() if key in pname or pname in key]
        if len(matches) == 1:
            resolved[name] = matches[0]
        else:
            log.warning("Parque não resolvido na API: %r (matches=%s)", name, matches)
    return resolved


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
    cutoff = (datetime.utcnow() - timedelta(minutes=cooldown_min)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE park = ? AND ride = ? AND sent_at > ? LIMIT 1",
        (park, ride, cutoff),
    ).fetchone()
    return row is not None


def mark_alerted(conn: sqlite3.Connection, park: str, ride: str) -> None:
    conn.execute(
        "INSERT INTO alerts_sent (park, ride, sent_at) VALUES (?, ?, ?)",
        (park, ride, datetime.utcnow().isoformat()),
    )
    conn.commit()


def get_threshold(park_cfg: dict, ride_name: str) -> int | None:
    """Threshold da atração; se não listada, usa default do parque apenas se watch_all."""
    attractions = park_cfg.get("attractions", {})
    for watched, threshold in attractions.items():
        if watched.lower() in ride_name.lower() or ride_name.lower() in watched.lower():
            return threshold
    return None


# ---------------------------------------------------------------- ciclo

def run_cycle(conn: sqlite3.Connection, config: dict, park_ids: dict[str, int]) -> None:
    ts = datetime.utcnow().isoformat()
    alert_parks = is_alert_day(config)
    cooldown = config.get("alert", {}).get("cooldown_minutes", 45)
    quiet = in_quiet_hours(config)

    for park_name, park_id in park_ids.items():
        try:
            payload = fetch_queue_times(park_id)
        except requests.RequestException as exc:
            log.error("Falha ao buscar %s: %s", park_name, exc)
            continue

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

    notifier.send("✅ Monitor de filas iniciado. Powered by Queue-Times.com")

    while True:
        try:
            run_cycle(conn, config, park_ids)
        except Exception:  # noqa: BLE001 — loop nunca deve morrer
            log.exception("Erro no ciclo de coleta")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
