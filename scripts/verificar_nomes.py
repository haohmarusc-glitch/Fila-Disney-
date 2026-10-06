"""Confere os nomes da watchlist contra o que a Queue-Times publica AGORA.

Existe porque nome de atração muda sozinho e o estrago é silencioso: o
`nome_watchlist` deixa de casar, a atração some do /status, do /perto, dos
alertas e do resumo, e nada no log diz que faltou alguém — nome que não casa é
indistinguível de atração fora da watchlist. O `tests/test_nomes_api.py` guarda
nomes congelados em 23/08/2026, que é outra pergunta: ele protege o
`normalizar_nome_api` contra regressão, não avisa que a API renomeou algo hoje.

O lembrete de 05/10 manda "rodar a verificação de nomes" — é este arquivo.

Uso, de dentro do container (que é quem tem rede e as dependências):

    docker compose exec fila-disney python scripts/verificar_nomes.py

Sai com código 1 se algum nome deixou de casar ou ficou ambíguo, para servir
num cron ou num CI com rede. Só lê: não escreve no banco nem na watchlist.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import monitor  # noqa: E402  (precisa do sys.path acima)


def conferir(park_cfg: dict, rides: list[dict]) -> dict:
    """(casadas, orfas, ambiguas) de um parque, sem tocar na rede.

    Separado do fetch de propósito: é esta parte que tem regra, e um teste
    precisa poder exercê-la com payload de mentira.
    """
    casadas: dict[str, list[str]] = {}
    for ride in rides:
        canonico = monitor.nome_watchlist(park_cfg, ride["name"])
        if canonico:
            casadas.setdefault(canonico, []).append(ride["name"])
    return {
        "casadas": casadas,
        "orfas": [nome for nome in park_cfg.get("attractions", {})
                  if nome not in casadas],
        "ambiguas": {k: v for k, v in casadas.items() if len(v) > 1},
    }


def rides_do_payload(payload: dict) -> list[dict]:
    return [ride for _land, ride in monitor.iter_rides(payload)]


def main() -> int:
    config = monitor.load_config()
    parques = list(config["parks"])
    ids = monitor.resolve_park_ids(parques)
    print(f"parques resolvidos: {len(ids)}/{len(parques)}")
    problemas = 0
    for parque in parques:
        if parque not in ids:
            print(f"  NAO RESOLVIDO na API: {parque}")
            problemas += 1
            continue
        park_cfg = config["parks"][parque]
        rides = rides_do_payload(monitor.fetch_queue_times(ids[parque]))
        achado = conferir(park_cfg, rides)
        print(f"[{parque}] API={len(rides)} "
              f"watchlist={len(park_cfg['attractions'])} "
              f"casadas={len(achado['casadas'])}")
        for nome in achado["orfas"]:
            print(f"  SEM CASAR (nome mudou ou atracao saiu): {nome}")
        for canonico, vindos in achado["ambiguas"].items():
            print(f"  AMBIGUA: {canonico} casa com {vindos}")
        problemas += len(achado["orfas"]) + len(achado["ambiguas"])
    print(f"\nproblemas de nome: {problemas}")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
