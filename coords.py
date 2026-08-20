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
import time
import unicodedata
from pathlib import Path

import monitor

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# A Overpass é um serviço público e gratuito, com política de uso moderado.
# Sete consultas seguidas sem pausa renderam 504, 429 e recusa de conexão na
# primeira execução real — daí a pausa entre parques e a espera longa no 429.
PAUSA_ENTRE_PARQUES_S = 20
TENTATIVAS_OVERPASS = 5
ESPERA_MINIMA_429_S = 45
RAIO_METROS = 1600  # cobre um parque inteiro com folga
COORDS_PATH = Path(__file__).parent / "coords.json"

# A API devolve latitude/longitude do parque, mas o dado tem erro: em 20/08/2026
# o Epic Universe veio com longitude +81.44 (sem o sinal), que cai no Nepal.
# Por isso todo parque passa por sanidade antes de virar centro de busca.
DISTANCIA_MAXIMA_KM = 100  # entre parques do mesmo complexo turístico

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


def coordenadas_sanas(parques: dict[str, tuple[float, float]]) -> tuple[dict, list[str]]:
    """Separa os parques com coordenada plausível dos que estão fora da curva.

    Sem âncora fixa de Orlando: usa a mediana dos próprios parques. Um ponto a
    mais de 100 km dos demais é erro de dado, não um parque distante — todos os
    monitorados ficam no mesmo complexo turístico.
    """
    if len(parques) < 3:
        return parques, []
    lats = sorted(c[0] for c in parques.values())
    lons = sorted(c[1] for c in parques.values())
    centro = (lats[len(lats) // 2], lons[len(lons) // 2])

    bons, suspeitos = {}, []
    for nome, coord in parques.items():
        km = monitor.distancia_metros(centro, coord) / 1000
        if km <= DISTANCIA_MAXIMA_KM:
            bons[nome] = coord
            continue
        aviso = f"{nome}: {coord} está a {km:,.0f} km dos outros parques"
        # Erro de sinal é o caso comum. Não corrijo sozinho, mas digo qual é o
        # conserto provável — quem decide é quem edita o coords.json.
        invertido = (coord[0], -coord[1])
        if monitor.distancia_metros(centro, invertido) / 1000 <= DISTANCIA_MAXIMA_KM:
            aviso += f"\n     Provável erro de sinal na API. O correto deve ser {invertido}"
        suspeitos.append(aviso)
    return bons, suspeitos


def buscar_osm(lat: float, lon: float) -> dict[str, tuple[float, float]]:
    """Atrações mapeadas no OSM ao redor do ponto: {nome normalizado: (lat, lon)}."""
    consulta = CONSULTA.format(raio=RAIO_METROS, lat=lat, lon=lon)
    # POST, não GET: a Overpass devolve 406 para consulta longa na URL
    resposta = monitor.post_json(
        OVERPASS_URL, {"data": consulta},
        tentativas=TENTATIVAS_OVERPASS, espera_minima=ESPERA_MINIMA_429_S)
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


def parque_completo(nome: str, config: dict, saida: dict) -> bool:
    """True se o coords.json já tem todas as atrações da watchlist deste parque.

    Existe para a re-execução ser barata: a Overpass é serviço público e não
    merece ser consultada de novo por um parque que já está resolvido.
    """
    desejadas = set(config["parks"].get(nome, {}).get("attractions", {}))
    return bool(desejadas) and desejadas <= set(saida.get("rides", {}).get(nome, {}))


def gravar(saida: dict) -> None:
    COORDS_PATH.write_text(
        json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    apenas_revisar = "--revisar" in sys.argv
    forcar = "--forcar" in sys.argv  # refaz até os parques já completos
    config = monitor.load_config()
    nomes = list(config["parks"])

    print("Buscando coordenadas dos parques em parks.json...")
    parques = coordenadas_dos_parques(nomes)
    if not parques:
        print("Nenhum parque com coordenada — nada a fazer.")
        return 1

    parques, suspeitos = coordenadas_sanas(parques)
    for aviso in suspeitos:
        print(f"  !! COORDENADA SUSPEITA, parque ignorado — {aviso}")
    if suspeitos:
        print("     (dado errado na API. Dá para corrigir à mão no coords.json,")
        print("      em \"parks\", e rodar de novo: o que já está lá é preservado.)")

    # Preserva o que já existe: correção manual não pode ser perdida na re-execução
    total_ok = total_falta = 0
    saida = monitor.load_coords()
    saida.setdefault("parks", {})
    saida.setdefault("rides", {})

    pendentes = []
    for nome, coord in parques.items():
        saida["parks"][nome] = list(coord)
        if parque_completo(nome, config, saida) and not forcar:
            print(f"=== {nome}: já completo no coords.json, pulando ===")
            total_ok += len(saida["rides"][nome])
            continue
        pendentes.append((nome, coord))

    for indice, (nome, (lat, lon)) in enumerate(pendentes):
        if indice:  # espaça as consultas: a Overpass pede uso moderado
            print(f"\n(aguardando {PAUSA_ENTRE_PARQUES_S}s antes da próxima consulta)")
            time.sleep(PAUSA_ENTRE_PARQUES_S)
        print(f"\n=== {nome} ({lat}, {lon}) ===")
        try:
            osm = buscar_osm(lat, lon)
        except Exception as exc:  # noqa: BLE001 — um parque falho não aborta o resto
            print(f"  !! Overpass falhou: {exc}")
            print("     Rode de novo mais tarde: o que já foi resolvido é preservado.")
            continue
        print(f"  {len(osm)} atrações mapeadas no OSM")

        saida["rides"].setdefault(nome, {})
        for atracao in config["parks"][nome].get("attractions", {}):
            resultado = casar(atracao, osm)
            if resultado is None:
                if atracao in saida["rides"][nome]:
                    print(f"  [ MANUAL] {atracao}  (mantida do coords.json)")
                    total_ok += 1
                else:
                    print(f"  [ FALTA ] {atracao}")
                    total_falta += 1
                continue
            osm_nome, confianca = resultado
            marca = "  [  OK  ]" if confianca >= 0.85 else "  [ CONF ]"
            print(f"{marca} {atracao}  ->  {osm_nome}  ({confianca:.2f})")
            saida["rides"][nome][atracao] = list(osm[osm_nome])
            total_ok += 1

        if not apenas_revisar:  # grava a cada parque: queda no meio não perde nada
            gravar(saida)

    print(f"\n{'=' * 60}\n{total_ok} com coordenada · {total_falta} sem")
    if apenas_revisar:
        print("--revisar: nada gravado.")
        return 0

    gravar(saida)
    print(f"Gravado em {COORDS_PATH}")
    print("As marcadas [ CONF ] merecem conferência; as [ FALTA ] podem ser")
    print("preenchidas à mão em coords.json, no formato \"Nome\": [lat, lon].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
