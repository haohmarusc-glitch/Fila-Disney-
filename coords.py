"""Enriquecimento de coordenadas — NÃO é etapa obrigatória do sistema.

O banco de coordenadas é o coords.json, versionado no repositório. O bot lê só
ele; a Overpass existe para PREENCHER esse arquivo, não para servi-lo. Se a
Overpass estiver fora, o que já está no coords.json continua valendo e o /perto
funciona igual — só não ganha coordenada nova.

Uso:
    docker compose exec fila-disney python coords.py --revisar   # só relatório
    docker compose exec fila-disney python coords.py             # grava coords.json
    docker compose exec fila-disney python coords.py --forcar    # refaz tudo

Parque já completo no coords.json é pulado, e o progresso é gravado a cada
parque: se a Overpass recusar no meio, é só rodar de novo mais tarde que ele
continua de onde parou. Falha total da Overpass não é erro de execução — o
script termina com 0 e diz o que ficou faltando.
"""
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import localizacao
import monitor

# Container Docker não tem IPv6 por padrão. O overpass-api.de resolve para IPv6
# e a conexão morre com "Network is unreachable" — foi o que travou a execução
# de 20/08/2026 depois das duas primeiras consultas terem calhado de ir por IPv4.
def forcar_ipv4() -> None:
    try:
        import socket
        import urllib3.util.connection as conexao_urllib3
        conexao_urllib3.allowed_gai_family = lambda: socket.AF_INET
    except Exception:  # noqa: BLE001 — sem urllib3 acessível, segue como estava
        pass


# Espelhos oficiais da Overpass. Se um recusa ou está fora, tenta o próximo em
# vez de insistir no mesmo — a fila de espera de cada instância é independente.
OVERPASS_ESPELHOS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)
OVERPASS_URL = OVERPASS_ESPELHOS[0]
# A Overpass é um serviço público e gratuito, com política de uso moderado.
# Sete consultas seguidas sem pausa renderam 504, 429 e recusa de conexão na
# primeira execução real — daí a pausa entre parques e a espera longa no 429.
PAUSA_ENTRE_PARQUES_S = 20
TENTATIVAS_OVERPASS = 5
ESPERA_MINIMA_429_S = 45
RAIO_METROS = 1600  # cobre um parque inteiro com folga
COORDS_PATH = monitor.COORDS_PATH  # data/, que é volume — sobrevive ao rebuild

# A API devolve latitude/longitude do parque, mas o dado tem erro: em 20/08/2026
# o Epic Universe veio com longitude +81.44 (sem o sinal), que cai no Nepal.
# Por isso todo parque passa por sanidade antes de virar centro de busca.
DISTANCIA_MAXIMA_KM = 100  # entre parques do mesmo complexo turístico

# Correção de dado errado de terceiro, aplicada SÓ quando o valor recebido falha
# na sanidade. Em 20/08/2026 o parks.json entregou o Epic Universe com longitude
# +81.44867409 (sem o sinal), que cai no Nepal. Se a API consertar, o valor bom
# passa na sanidade e esta tabela nunca é consultada.
CORRECOES_COORDENADA = {
    "Universal Epic Universe": (28.44144545, -81.44867409),
}

# relation entra porque atração grande costuma ser multipolígono no OSM: sem
# ela, VelociCoaster, Spider-Man, Ripsaw Falls e Forbidden Journey não voltavam
# nem como candidato — e "Ripsaw Falls" dentro de "Dudley Do-Right's Ripsaw
# Falls" casaria com 0.85 se estivesse na resposta. `out center` resolve o
# centro de way e relation igual.
CONSULTA = """
[out:json][timeout:90];
(
  node(around:{raio},{lat},{lon})["attraction"]["name"];
  way(around:{raio},{lat},{lon})["attraction"]["name"];
  relation(around:{raio},{lat},{lon})["attraction"]["name"];
  node(around:{raio},{lat},{lon})["tourism"="attraction"]["name"];
  way(around:{raio},{lat},{lon})["tourism"="attraction"]["name"];
  relation(around:{raio},{lat},{lon})["tourism"="attraction"]["name"];
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


def candidatos_proximos(alvo: str, candidatos: dict, quantos: int = 3) -> list[tuple[str, float]]:
    """Os nomes do OSM mais parecidos, ignorando o corte de confiança.

    Serve para o log de FALTA ser acionável: sem isso sobra "não achei" e a
    pessoa tem que garimpar o OSM na mão para descobrir como a atração se chama
    lá. Mesma ideia do aviso de parque não resolvido.
    """
    alvo_tokens = tokens(alvo)
    if not alvo_tokens:
        return []
    pontuados = []
    for nome in candidatos:
        outros = tokens(nome)
        if outros:
            pontuados.append((len(alvo_tokens & outros) / len(alvo_tokens | outros), nome))
    pontuados.sort(reverse=True)
    return [(nome, score) for score, nome in pontuados[:quantos] if score > 0]


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
        km = localizacao.distancia_metros(centro, coord) / 1000
        if km <= DISTANCIA_MAXIMA_KM:
            bons[nome] = coord
            continue

        # Só aqui, com o dado comprovadamente fora da curva, vale substituir.
        correcao = CORRECOES_COORDENADA.get(nome)
        if correcao and localizacao.distancia_metros(centro, correcao) / 1000 <= DISTANCIA_MAXIMA_KM:
            print(f"  ** {nome}: coordenada da API está errada ({coord}); "
                  f"usando a correção conhecida {correcao}")
            bons[nome] = correcao
            continue
        aviso = f"{nome}: {coord} está a {km:,.0f} km dos outros parques"
        # Erro de sinal é o caso comum. Não corrijo sozinho, mas digo qual é o
        # conserto provável — quem decide é quem edita o coords.json.
        invertido = (coord[0], -coord[1])
        if localizacao.distancia_metros(centro, invertido) / 1000 <= DISTANCIA_MAXIMA_KM:
            aviso += f"\n     Provável erro de sinal na API. O correto deve ser {invertido}"
        suspeitos.append(aviso)
    return bons, suspeitos


def buscar_osm(lat: float, lon: float) -> dict[str, tuple[float, float]]:
    """Atrações mapeadas no OSM ao redor do ponto: {nome normalizado: (lat, lon)}."""
    consulta = CONSULTA.format(raio=RAIO_METROS, lat=lat, lon=lon)
    # POST, não GET: a Overpass devolve 406 para consulta longa na URL
    erros = []
    for espelho in OVERPASS_ESPELHOS:
        try:
            resposta = monitor.post_json(
                espelho, {"data": consulta},
                tentativas=TENTATIVAS_OVERPASS, espera_minima=ESPERA_MINIMA_429_S)
            break
        except Exception as exc:  # noqa: BLE001 — espelho fora, tenta o próximo
            print(f"  .. espelho {espelho.split('/')[2]} indisponível")
            erros.append(f"{espelho.split('/')[2]}: {exc}")
    else:
        raise RuntimeError("nenhum espelho da Overpass respondeu:\n     "
                           + "\n     ".join(erros))
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
    COORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COORDS_PATH.write_text(
        json.dumps(saida, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    forcar_ipv4()
    apenas_revisar = "--revisar" in sys.argv
    forcar = "--forcar" in sys.argv  # refaz até os parques já completos
    listar = "--listar" in sys.argv  # despeja os nomes crus do OSM e sai
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
    total_ok = total_falta = falhas_overpass = 0
    saida = localizacao.load_coords()
    saida.setdefault("parks", {})
    saida.setdefault("rides", {})
    saida.setdefault("aliases", {})  # nome da watchlist -> nome no OSM, editável

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
            falhas_overpass += 1
            continue
        print(f"  {len(osm)} atrações mapeadas no OSM")
        if listar:  # conferir o que existe de fato, em vez de supor
            for osm_nome in sorted(osm):
                print(f"       {osm_nome}")
            continue

        saida["rides"].setdefault(nome, {})
        for atracao in config["parks"][nome].get("attractions", {}):
            apelido = saida.get("aliases", {}).get(atracao)
            if apelido and normalizar(apelido) in osm:
                saida["rides"][nome][atracao] = list(osm[normalizar(apelido)])
                print(f"  [ ALIAS ] {atracao}  ->  {normalizar(apelido)}")
                total_ok += 1
                continue
            resultado = casar(atracao, osm)
            if resultado is None:
                if atracao in saida["rides"][nome]:
                    print(f"  [ MANUAL] {atracao}  (mantida do coords.json)")
                    total_ok += 1
                else:
                    print(f"  [ FALTA ] {atracao}")
                    for candidato, score in candidatos_proximos(atracao, osm):
                        print(f"             candidato no OSM: {candidato}  ({score:.2f})")
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

    if total_ok:
        print()
        print("O coords.json já serve: o /perto usa o que estiver aqui e ignora o")
        print("resto. Commite o arquivo para virar o banco local do projeto.")
    if falhas_overpass:
        print()
        print(f"A Overpass não respondeu para {falhas_overpass} parque(s). Isso NÃO")
        print("é falha do sistema: rode de novo mais tarde para completar, ou")
        print("preencha à mão. O bot funciona com o que já existe.")
    return 0  # Overpass fora nunca é erro de execução


if __name__ == "__main__":
    sys.exit(main())
