"""Busca coordenadas das atrações no OpenStreetMap e grava coords.json.

Roda UMA VEZ (ou quando a watchlist mudar). Não faz parte do loop do monitor:
o bot só lê o coords.json pronto.

    docker compose exec fila-disney python coords.py
    docker compose exec fila-disney python coords.py --revisar   # só relatório

Por que OSM: o Queue-Times não devolve lat/lon (confirmado no queue_times.json,
que traz só id, name, is_open, wait_time e last_updated). Os parques de Orlando
são bem mapeados no OSM, e a Overpass API é aberta e sem chave.

O casamento de nomes entre OSM e Queue-Times é aproximado. O que não casar sai
listado para ajuste manual — é melhor faltar coordenada do que ter coordenada
errada mandando o grupo para o outro lado do parque.
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

import monitor

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
RAIO_METROS = 1600  # cobre um parque inteiro com folga
COORDS_PATH = Path(__file__).parent / "coords.json"

CONSULTA = """
[out:json][timeout:90];
(
  node(around:{raio},{lat},{lon})["attraction"]["name"];
  way(around:{raio},{lat},{lon})["attraction"]["name"];
  node(around:{raio},{lat},{lon})["tourism"="attraction"]["name"];
  way(around:{raio},{lat},{lon})["tourism"="attraction"]["name"];
);
out center;
"""

# Ruído que aparece num dos lados e não no outro
SUFIXOS = (
    "starring aerosmith", "presented by chevrolet", "at walt disney presents",
    "a musical adventure", "the ride 3d", "the ride", "adventure", "experience",
)


def normalizar(nome: str) -> str:
    """Minúsculo, sem acento, sem pontuação, sem sufixo de patrocínio."""
    texto = unicodedata.normalize("NFKD", nome.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    for sufixo in SUFIXOS:
        if texto.endswith(sufixo) and len(texto) > len(sufixo) + 3:
            texto = texto[: -len(sufixo)].strip()
    return texto


def tokens(nome: str) -> set[str]:
    ignorar = {"the", "of", "and", "a", "at", "de", "da", "do"}
    return {t for t in normalizar(nome).split() if t not in ignorar and len(t) > 2}


def casar(alvo: str, candidatos: dict[str, tuple[float, float]]) -> tuple[str, float] | None:
    """(nome no OSM, confiança 0-1) ou None. Confiança < 0.6 é descartada."""
    alvo_norm = normalizar(alvo)
    if alvo_norm in candidatos:
        return alvo_norm, 1.0

    for nome in candidatos:
        if not alvo_norm or not (alvo_norm in nome or nome in alvo_norm):
            continue
        # conter o nome inteiro é evidência forte; o comprimento só refina.
        # Exige 2+ palavras para "epcot" não casar dentro de qualquer coisa.
        curto = alvo_norm if len(alvo_norm) <= len(nome) else nome
        if len(curto.split()) < 2:
            continue
        menor, maior = sorted((len(alvo_norm), len(nome)))
        return nome, 0.75 + 0.25 * menor / maior

    alvo_tokens = tokens(alvo)
    if not alvo_tokens:
        return None
    melhor, melhor_score = None, 0.0
    for nome in candidatos:
        outros = tokens(nome)
        if not outros:
            continue
        score = len(alvo_tokens & outros) / len(alvo_tokens | outros)
        if score > melhor_score:
            melhor, melhor_score = nome, score
    return (melhor, melhor_score) if melhor and melhor_score >= 0.6 else None


def buscar_osm(lat: float, lon: float) -> dict[str, tuple[float, float]]:
    """Atrações mapeadas no OSM ao redor do ponto: {nome normalizado: (lat, lon)}."""
    consulta = CONSULTA.format(raio=RAIO_METROS, lat=lat, lon=lon)
    resposta = monitor.get_json(f"{OVERPASS_URL}?data={consulta}")
    achados: dict[str, tuple[float, float]] = {}
    for elemento in resposta.get("elements", []):
        nome = elemento.get("tags", {}).get("name")
        if not nome:
            continue
        centro = elemento.get("center") or elemento
        if "lat" not in centro or "lon" not in centro:
            continue
        achados[normalizar(nome)] = (round(centro["lat"], 6), round(centro["lon"], 6))
    return achados


def coordenadas_dos_parques(nomes: list[str]) -> dict[str, tuple[float, float]]:
    """parks.json TEM lat/lon do parque — é o centro da busca no OSM."""
    grupos = monitor.get_json(monitor.PARKS_URL)
    disponiveis = {}
    for grupo in grupos:
        for parque in grupo.get("parks", []):
            lat, lon = parque.get("latitude"), parque.get("longitude")
            if lat and lon:
                disponiveis[parque["name"].strip().lower()] = (float(lat), float(lon))

    resolvidos = {}
    for nome in nomes:
        chave = nome.strip().lower()
        if chave in disponiveis:
            resolvidos[nome] = disponiveis[chave]
            continue
        parciais = [k for k in disponiveis if chave in k or k in chave]
        if len(parciais) == 1:
            resolvidos[nome] = disponiveis[parciais[0]]
        else:
            print(f"  !! sem coordenada do parque: {nome}")
    return resolvidos


def main() -> int:
    apenas_revisar = "--revisar" in sys.argv
    config = monitor.load_config()
    nomes = list(config["parks"])

    print("Buscando coordenadas dos parques em parks.json...")
    parques = coordenadas_dos_parques(nomes)
    if not parques:
        print("Nenhum parque com coordenada — nada a fazer.")
        return 1

    saida = {"parks": {}, "rides": {}}
    total_ok = total_falta = 0

    for nome, (lat, lon) in parques.items():
        print(f"\n=== {nome} ({lat}, {lon}) ===")
        saida["parks"][nome] = [lat, lon]
        try:
            osm = buscar_osm(lat, lon)
        except Exception as exc:  # noqa: BLE001 — um parque falho não aborta o resto
            print(f"  !! Overpass falhou: {exc}")
            continue
        print(f"  {len(osm)} atrações mapeadas no OSM")

        saida["rides"][nome] = {}
        for atracao in config["parks"][nome].get("attractions", {}):
            resultado = casar(atracao, osm)
            if resultado is None:
                print(f"  [ FALTA ] {atracao}")
                total_falta += 1
                continue
            osm_nome, confianca = resultado
            marca = "  [  OK  ]" if confianca >= 0.85 else "  [ CONF ]"
            print(f"{marca} {atracao}  ->  {osm_nome}  ({confianca:.2f})")
            saida["rides"][nome][atracao] = list(osm[osm_nome])
            total_ok += 1

    print(f"\n{'=' * 60}\n{total_ok} com coordenada · {total_falta} sem")
    if apenas_revisar:
        print("--revisar: nada gravado.")
        return 0

    COORDS_PATH.write_text(
        json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Gravado em {COORDS_PATH}")
    print("As marcadas [ CONF ] merecem conferência; as [ FALTA ] podem ser")
    print("preenchidas à mão em coords.json, no formato \"Nome\": [lat, lon].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
