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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "data" / "history.db"

TZ_PARQUE = ZoneInfo("America/New_York")
# Agrupa por (dia, hora) em UTC e converte cada balde depois. Um offset fixo
# erraria: em 01/11/2026 Orlando volta ao EST, e um "-4" cravado deslocaria em
# 1h todo o histórico de outubro assim que a análise fosse rodada em novembro.
DIA_HORA_UTC = "date(ts) AS d, CAST(strftime('%H', ts) AS INTEGER) AS h"


def hora_local(dia_utc: str, hora_utc: int) -> int:
    """Hora no fuso do parque para um balde (dia, hora) gravado em UTC."""
    momento = datetime.fromisoformat(f"{dia_utc}T{hora_utc:02d}:00:00+00:00")
    return momento.astimezone(TZ_PARQUE).hour


def agregar_por_hora_local(linhas) -> dict[int, tuple[float, int]]:
    """[(dia, hora, media, n)] -> {hora local: (média ponderada, n)}.

    Ponderado pela contagem: dois baldes com número de leituras diferente não
    podem pesar igual só porque caíram na mesma hora local.
    """
    acumulado: dict[int, tuple[float, int]] = {}
    for dia, hora, media, n in linhas:
        if media is None or not n:
            continue
        local = hora_local(dia, hora)
        soma, total = acumulado.get(local, (0.0, 0))
        acumulado[local] = (soma + media * n, total + n)
    return {h: (soma / total, total) for h, (soma, total) in acumulado.items()}


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
        SELECT ride, {DIA_HORA_UTC}, AVG(wait_time), COUNT(*)
        FROM wait_times
        WHERE is_open = 1 AND wait_time IS NOT NULL AND park LIKE ?
        GROUP BY ride, d, h
        """,
        (f"%{park}%",),
    ).fetchall()
    por_atracao: dict[str, list] = {}
    for ride, dia, hora, media, n in rows:
        por_atracao.setdefault(ride, []).append((dia, hora, media, n))
    for ride in sorted(por_atracao):
        serie = agregar_por_hora_local(por_atracao[ride])
        if not serie:
            continue
        melhor = min(serie.items(), key=lambda item: item[1][0])
        pior = max(serie.items(), key=lambda item: item[1][0])
        print(f"{ride:<55} melhor {melhor[0]:02d}h ({melhor[1][0]:.1f} min) "
              f"| pior {pior[0]:02d}h ({pior[1][0]:.1f} min)")


def ride_detail(conn: sqlite3.Connection, park: str, ride: str) -> None:
    rows = conn.execute(
        f"""
        SELECT {DIA_HORA_UTC}, AVG(wait_time), COUNT(*)
        FROM wait_times
        WHERE is_open = 1 AND wait_time IS NOT NULL AND park LIKE ? AND ride LIKE ?
        GROUP BY d, h
        """,
        (f"%{park}%", f"%{ride}%"),
    ).fetchall()
    print(f"Média de espera por hora (horário do parque) — {ride} @ {park}")
    for hora, (media, n) in sorted(agregar_por_hora_local(rows).items()):
        barra = "#" * int(media // 5)
        print(f"  {hora:02d}h  {media:>6.1f} min  {barra}  (n={n})")


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
