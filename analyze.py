"""
Análise pré-viagem do histórico coletado (data/history.db).

Uso:
    python analyze.py                     # resumo de todos os parques
    python analyze.py "Epcot"             # detalhe de um parque
    python analyze.py "Epcot" "Frozen"    # melhor horário de uma atração

Saída: média de espera por hora do dia (horário do parque, America/New_York),
para você planejar rope drop, almoço e fim de tarde antes da viagem.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "history.db"

# SQLite guarda ts em UTC; Orlando = UTC-4 (EDT) em outubro.
HOUR_EXPR = "CAST(strftime('%H', ts) AS INTEGER)"
TZ_OFFSET = -4


def park_hour(utc_hour: int) -> int:
    return (utc_hour + TZ_OFFSET) % 24


def summary(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT park, ride, ROUND(AVG(wait_time), 1) AS avg_wait, MAX(wait_time) AS max_wait,
               COUNT(*) AS samples
        FROM wait_times
        WHERE is_open = 1 AND wait_time IS NOT NULL
        GROUP BY park, ride
        ORDER BY park, avg_wait DESC
        """
    ).fetchall()
    current = None
    for park, ride, avg_wait, max_wait, samples in rows:
        if park != current:
            print(f"\n=== {park} ===")
            current = park
        print(f"  {ride:<55} média {avg_wait:>6} min | máx {max_wait:>4} | n={samples}")


def park_detail(conn: sqlite3.Connection, park: str) -> None:
    rows = conn.execute(
        f"""
        SELECT ride, {HOUR_EXPR} AS h, ROUND(AVG(wait_time), 1)
        FROM wait_times
        WHERE is_open = 1 AND wait_time IS NOT NULL AND park LIKE ?
        GROUP BY ride, h ORDER BY ride, h
        """,
        (f"%{park}%",),
    ).fetchall()
    by_ride: dict[str, list[tuple[int, float]]] = {}
    for ride, h, avg in rows:
        by_ride.setdefault(ride, []).append((park_hour(h), avg))
    for ride, series in by_ride.items():
        series.sort()
        best = min(series, key=lambda x: x[1])
        worst = max(series, key=lambda x: x[1])
        print(f"{ride:<55} melhor {best[0]:02d}h ({best[1]} min) | pior {worst[0]:02d}h ({worst[1]} min)")


def ride_detail(conn: sqlite3.Connection, park: str, ride: str) -> None:
    rows = conn.execute(
        f"""
        SELECT {HOUR_EXPR} AS h, ROUND(AVG(wait_time), 1), COUNT(*)
        FROM wait_times
        WHERE is_open = 1 AND wait_time IS NOT NULL AND park LIKE ? AND ride LIKE ?
        GROUP BY h ORDER BY h
        """,
        (f"%{park}%", f"%{ride}%"),
    ).fetchall()
    print(f"Média de espera por hora (horário do parque) — {ride} @ {park}")
    for h, avg, n in sorted(rows, key=lambda r: park_hour(r[0])):
        bar = "#" * int(avg // 5)
        print(f"  {park_hour(h):02d}h  {avg:>6} min  {bar}  (n={n})")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit("Sem histórico ainda — rode o monitor primeiro.")
    conn = sqlite3.connect(DB_PATH)
    args = sys.argv[1:]
    if len(args) == 0:
        summary(conn)
    elif len(args) == 1:
        park_detail(conn, args[0])
    else:
        ride_detail(conn, args[0], args[1])


if __name__ == "__main__":
    main()
