"""Healthcheck do container: a última coleta é recente?

Sai 0 se sim, 1 se não. Usado pelo HEALTHCHECK do Dockerfile — sem isso o
container fica "up" mesmo com o loop travado sem gravar nada.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "history.db"
TOLERANCIA_MIN = 15  # 3 ciclos de 5 min


def main() -> int:
    if not DB_PATH.exists():
        print("banco ainda não criado")
        return 1
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        ultima = conn.execute("SELECT MAX(ts) FROM wait_times").fetchone()[0]
    except sqlite3.Error as exc:
        print(f"erro no banco: {exc}")
        return 1
    if not ultima:
        print("sem nenhuma leitura")
        return 1
    agora = datetime.now(timezone.utc).replace(tzinfo=None)
    atraso = agora - datetime.fromisoformat(ultima)
    if atraso > timedelta(minutes=TOLERANCIA_MIN):
        print(f"última coleta há {atraso.total_seconds() / 60:.0f} min")
        return 1
    print(f"ok, última coleta há {atraso.total_seconds() / 60:.0f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
