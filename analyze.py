"""
Análise pré-viagem do histórico coletado (data/history.db).

Uso:
    python analyze.py                     # resumo de todos os parques
    python analyze.py "Epcot"             # detalhe de um parque
    python analyze.py "Epcot" "Frozen"    # melhor horário de uma atração
    python analyze.py --idade             # quão velho é o dado da fonte, por parque

Saída: média de espera por hora do dia (horário do parque, America/New_York),
para você planejar rope drop, almoço e fim de tarde antes da viagem.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import monitor

DB_PATH = Path(__file__).parent / "data" / "history.db"

# Agrupa por (dia, hora) em UTC e converte cada balde depois, com a MESMA função
# que o monitor usa no resumo das 7h. Um offset fixo erraria: em 01/11/2026
# Orlando volta ao EST, e um "-4" cravado deslocaria em 1h todo o histórico de
# outubro assim que a análise fosse rodada em novembro.
DIA_HORA_UTC = "date(ts) AS d, CAST(strftime('%H', ts) AS INTEGER) AS h"


def fuso() -> ZoneInfo:
    """O fuso do watchlist.json; a análise não pode divergir do monitor."""
    try:
        return monitor.fuso_do_parque(monitor.load_config())
    except (OSError, KeyError, ValueError):
        return ZoneInfo("America/New_York")


def hora_local(dia_utc: str, hora_utc: int) -> int:
    return monitor.hora_no_parque(dia_utc, hora_utc, fuso())


def agregar_por_hora_local(linhas) -> dict[int, tuple[float, int]]:
    return monitor.agregar_por_hora_local(linhas, fuso())


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


def frescor(conn: sqlite3.Connection) -> None:
    """Quão velho é o dado que a Queue-Times nos entrega, por parque.

    `ts` é quando coletamos; `source_updated_at` é o `last_updated` que a fonte
    publicou junto. A diferença é a idade do dado — e ela não é uniforme: medido
    em 06/09/2026 com 8h de coleta, os quatro parques Disney tinham pior caso de
    7 min, enquanto Islands of Adventure chegava a 1094 min e Universal Studios
    a 2029. A mediana de ~3 min em todos é só a fase do nosso ciclo de 5 min,
    não qualidade da fonte: o problema mora inteiro na cauda.

    A segunda tabela é a que decide se isso importa. A maioria das leituras
    velhas está marcada como aberta mas com `wait_time` 0 — filas paralelas e
    shows, que a regra 10 já descarta de tudo que o usuário vê. O que
    contamina o perfil histórico são só as velhas COM fila positiva.

    Existe para ser rodado de novo depois de mais dias, com o mesmo critério:
    a decisão de filtrar (ou não) a previsão sai destes números, e refazer a
    conta de cabeça em outra ocasião daria outro corte.
    """
    por_parque: dict[str, list] = {}
    medidas = 0
    for park, ts, src, aberta, wait in conn.execute(
        "SELECT park, ts, source_updated_at, is_open, wait_time FROM wait_times "
        "WHERE source_updated_at IS NOT NULL"
    ):
        try:
            idade = (datetime.fromisoformat(ts)
                     - datetime.fromisoformat(src.replace("Z", ""))).total_seconds() / 60
        except ValueError:  # carimbo da fonte fora do ISO: não é medível
            continue
        por_parque.setdefault(park, []).append((idade, aberta, wait))
        medidas += 1

    # Conta o que deu para MEDIR, não o que tem carimbo. Os dois divergem
    # quando a fonte manda data ilegível, e anunciar "n leituras" acima de uma
    # tabela vazia é o tipo de saída que faz duvidar do banco em vez do parser.
    if not medidas:
        print("Nenhuma leitura com idade medível — a coluna é de 06/09/2026 e as")
        print("linhas anteriores ficam NULL de propósito: não sabemos a idade delas.")
        return

    print(f"Idade do dado na origem — {medidas:,} leituras medidas")
    print(f"{'parque':<42}{'n':>7}{'mediana':>9}{'p90':>7}{'pior':>8}")
    for park in sorted(por_parque):
        idades = sorted(i for i, _a, _w in por_parque[park])
        n = len(idades)
        print(f"{park:<42}{n:>7}{idades[n // 2]:>8.0f}m"
              f"{idades[int(n * 0.9)]:>6.0f}m{idades[-1]:>7.0f}m")

    limite = monitor.OBSOLETO_MINUTOS_PADRAO
    print(f"\nLeituras com mais de {limite} min de idade — só as com fila positiva")
    print("contaminam o perfil histórico; as de fila 0 já são descartadas (regra 10).")
    print(f"{'parque':<42}{'velhas':>8}{'abertas':>9}{'c/fila>0':>10}")
    for park in sorted(por_parque):
        velhas = [(a, w) for i, a, w in por_parque[park] if i > limite]
        if not velhas:
            continue
        abertas = [(a, w) for a, w in velhas if a]
        com_fila = [w for a, w in abertas if w]
        print(f"{park:<42}{len(velhas):>8}{len(abertas):>9}{len(com_fila):>10}")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit("Sem histórico ainda — rode o monitor primeiro.")
    conn = sqlite3.connect(DB_PATH)
    args = sys.argv[1:]
    if args and args[0] == "--idade":
        frescor(conn)
    elif len(args) == 0:
        summary(conn)
    elif len(args) == 1:
        park_detail(conn, args[0])
    else:
        ride_detail(conn, args[0], args[1])


if __name__ == "__main__":
    main()
