"""Encontros com personagens próximos, sem confundir ponto oficial com GPS ao vivo."""
from __future__ import annotations

import math
import re
import unicodedata
from urllib.parse import quote_plus


RAIO_PADRAO_METROS = 500
COOLDOWN_MINUTOS = 60

# Quando o encontro não existe em coords.json, usa-se uma atração vizinha apenas
# para decidir proximidade. O botão do Maps busca o nome oficial do encontro.
ANCHORS = {
    "Disney Magic Kingdom": {
        "meet ariel at her grotto": "Seven Dwarfs Mine Train",
        "meet cinderella and a visiting princess at princess fairytale hall": "Seven Dwarfs Mine Train",
        "meet princess tiana and a visiting princess at princess fairytale hall": "Seven Dwarfs Mine Train",
        "meet daring disney pals as circus stars at pete s silly sideshow": "Space Mountain",
        "meet dashing disney pals as circus stars at pete s silly sideshow": "Space Mountain",
        "meet mickey at town square theater": "Pirates of the Caribbean",
    },
    "Epcot": {
        "meet beloved disney pals at mickey friends": "Guardians of the Galaxy: Cosmic Rewind",
        "meet anna and elsa at royal sommerhus": "Frozen Ever After",
    },
    "Disney Hollywood Studios": {
        "meet ariel at walt disney presents": "Toy Story Mania!",
        "meet disney stars at red carpet dreams": "Mickey & Minnie's Runaway Railway",
        "meet olaf at celebrity spotlight": "Mickey & Minnie's Runaway Railway",
        "meet edna mode at the edna mode experience": "Toy Story Mania!",
    },
    "Disney Animal Kingdom": {
        "meet favorite disney pals at adventurers outpost": "Kilimanjaro Safaris",
        "meet moana at character landing": "Kilimanjaro Safaris",
    },
    "Universal Epic Universe": {
        "meet toothless and friends": "Hiccup's Wing Gliders",
    },
}


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def eh_encontro(nome: str) -> bool:
    value = normalize(nome)
    return value.startswith("meet ") or "character encounter" in value


def iter_encontros(payload: dict):
    for land in payload.get("lands", []):
        for ride in land.get("rides", []):
            if eh_encontro(str(ride.get("name", ""))):
                yield ride


def coordenada_encontro(park: str, nome: str, coords: dict):
    rides = coords.get("rides", {}).get(park, {})
    target = normalize(nome)
    exact = next((point for key, point in rides.items() if normalize(key) == target), None)
    if exact:
        return tuple(exact)
    anchor_name = ANCHORS.get(park, {}).get(target)
    if not anchor_name:
        return None
    anchor_target = normalize(anchor_name)
    for key, point in rides.items():
        key_normalized = normalize(key)
        if key_normalized == anchor_target or anchor_target in key_normalized or key_normalized in anchor_target:
            return tuple(point)
    return None


def distancia_metros(a, b) -> float:
    radius = 6_371_000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def proximos(position, park: str, payload: dict, coords: dict,
             raio: int = RAIO_PADRAO_METROS) -> list[dict]:
    result = []
    for ride in iter_encontros(payload):
        if not ride.get("is_open"):
            continue
        nome = str(ride.get("name", ""))
        coord = coordenada_encontro(park, nome, coords)
        if coord is None:
            continue
        meters = round(distancia_metros(position, coord))
        if meters > raio:
            continue
        wait = ride.get("wait_time")
        wait = int(wait) if isinstance(wait, (int, float)) and math.isfinite(wait) else None
        result.append({
            "name": nome,
            "wait": wait,
            "meters": meters,
            "walk": max(1, round(meters * 1.25 / 80)),
            "coordinate": coord,
        })
    return sorted(result, key=lambda item: (item["meters"], item["wait"] or 0))


def maps_url(nome: str, park: str) -> str:
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{nome}, {park}, Orlando")
