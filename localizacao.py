"""Localização, rota e ranking por tempo total.

Separado do monitor porque é o único bloco que fala de geografia: coordenada,
distância, caminhada e score. O monitor não importa nada daqui em runtime — se
o coords.json não existir, tudo aqui degrada para "sem estimativa" e o resto do
bot funciona igual.

Nenhuma chamada a serviço externo acontece aqui. As coordenadas vêm do
coords.json, gerado uma vez pelo coords.py; a Overpass não é dependência de
execução, só de geração.
"""
import json
import logging
import math
import os
import time

import monitor
import notifier

log = logging.getLogger("localizacao")

VELOCIDADE_M_POR_MIN = 84      # 5 km/h
FATOR_CAMINHO = 1.3            # linha reta vira caminho real dentro do parque
RAIO_PARQUE_METROS = 2500      # além disso você não está nesse parque
MAPS_URL = ("https://www.google.com/maps/dir/?api=1"
            "&origin={o_lat},{o_lon}&destination={d_lat},{d_lon}&travelmode=walking")
GOOGLE_ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
ROTA_CACHE_TTL = 300
_rota_cache = {}

# Pesos do score. NÃO SÃO CALIBRADOS: são um ponto de partida razoável, não o
# resultado de backtest. Ficam em watchlist.json para poder mudar sem deploy, e
# a intenção é recalibrá-los em setembro, quando houver semanas de histórico.
PESOS_PADRAO = {"tempo": 0.5, "historico": 0.3, "tendencia": 0.2}


def load_coords() -> dict:
    """coords.json é opcional: sem ele o bot roda igual, só sem /perto."""
    try:
        caminho = (monitor.COORDS_PATH if monitor.COORDS_PATH.exists()
                   else monitor.COORDS_PATH_REPO)
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"parks": {}, "rides": {}}


def distancia_metros(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine. Em escala de parque o erro é irrelevante e não precisa de lib."""
    raio = 6_371_000
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * raio * math.asin(math.sqrt(h))


def minutos_a_pe(metros: float) -> int:
    """Estimativa, não rota. Google Maps não mapeia caminho interno de parque."""
    return max(1, round(metros * FATOR_CAMINHO / VELOCIDADE_M_POR_MIN))


def _chave_cache(posicao, destinos):
    # ~55 m: reaproveita consultas feitas praticamente do mesmo lugar.
    origem = (round(posicao[0], 3), round(posicao[1], 3))
    return origem, tuple((nome, tuple(coord)) for nome, coord in destinos)


def rotas_google(posicao, destinos):
    """Retorna {nome: (minutos, metros)} ou {} se a API não estiver configurada.

    Compute Route Matrix cobra por elemento. Todas as atrações elegíveis são
    enviadas para que a estimativa em linha reta não decida o ranking antes da
    rota real. Respostas parciais conservam o fallback de cada atração ausente.
    """
    if not GOOGLE_MAPS_API_KEY or not destinos:
        return {}
    chave = _chave_cache(posicao, destinos)
    agora = time.monotonic()
    armazenado = _rota_cache.get(chave)
    if armazenado and agora - armazenado[0] < ROTA_CACHE_TTL:
        return armazenado[1]

    corpo = {
        "origins": [{"waypoint": {"location": {"latLng": {
            "latitude": posicao[0], "longitude": posicao[1]}}}}],
        "destinations": [{"waypoint": {"location": {"latLng": {
            "latitude": coord[0], "longitude": coord[1]}}}}
                         for _nome, coord in destinos],
        "travelMode": "WALK",
        "languageCode": "pt-BR",
        "units": "METRIC",
    }
    headers = {
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,status,condition",
    }
    try:
        resposta = monitor.post_json_body(GOOGLE_ROUTES_URL, corpo,
                                          cabecalhos=headers, tentativas=2)
    except Exception as exc:  # localização nunca pode derrubar o bot
        log.warning("Routes API indisponível; usando estimativa: %s", exc)
        return {}
    if not isinstance(resposta, list):
        log.warning("Routes API devolveu formato inesperado: %s", type(resposta).__name__)
        return {}
    saida = {}
    for elemento in resposta:
        indice = elemento.get("destinationIndex")
        if not isinstance(indice, int) or not (0 <= indice < len(destinos)):
            continue
        if elemento.get("condition") not in (None, "ROUTE_EXISTS"):
            continue
        segundos = str(elemento.get("duration", "")).removesuffix("s")
        try:
            minutos = max(1, math.ceil(float(segundos) / 60))
            metros = int(elemento["distanceMeters"])
        except (KeyError, TypeError, ValueError):
            continue
        saida[destinos[indice][0]] = (minutos, metros)
    _rota_cache[chave] = (agora, saida)
    return saida


def score_oportunidade(total: int, melhor_total: int, pior_total: int,
                       desvio_historico: float | None, seta: str | None,
                       pesos: dict) -> int:
    """0 a 100 combinando tempo total, desvio da média histórica e tendência.

    Cada componente é normalizado para 0..1 antes de entrar com seu peso, então
    o número é comparável entre atrações do mesmo ranking — e só entre elas.
    Não é probabilidade nem nota absoluta: 80 aqui não significa nada fora
    desta lista.
    """
    # tempo: melhor do ranking = 1, pior = 0
    faixa = max(pior_total - melhor_total, 1)
    componente_tempo = 1 - (total - melhor_total) / faixa

    # histórico: quanto a fila está abaixo da média daquela atração
    if desvio_historico is None:
        componente_hist = 0.5  # sem histórico, nem premia nem pune
    else:
        componente_hist = min(max(0.5 + desvio_historico / 2, 0.0), 1.0)

    componente_tend = {"↓": 1.0, "→": 0.5, "↑": 0.0}.get(seta, 0.5)

    bruto = (pesos.get("tempo", 0.5) * componente_tempo
             + pesos.get("historico", 0.3) * componente_hist
             + pesos.get("tendencia", 0.2) * componente_tend)
    return round(bruto * 100)


def desvio_da_media(conn, park: str, ride: str, fila_agora: int) -> float | None:
    """Quanto a fila de agora está abaixo da média histórica, de -1 a 1.

    Positivo quer dizer melhor que o normal. None quando não há histórico
    suficiente — melhor não opinar do que opinar com duas leituras.
    """
    if conn is None:
        return None
    linha = conn.execute(
        "SELECT AVG(wait_time), COUNT(*) FROM wait_times "
        "WHERE park = ? AND ride = ? AND is_open = 1 AND wait_time IS NOT NULL",
        (park, ride),
    ).fetchone()
    if not linha or not linha[0] or linha[1] < 12:  # ~1h de coleta
        return None
    media = linha[0]
    return max(min((media - fila_agora) / media, 1.0), -1.0)


def parque_mais_proximo(posicao: tuple[float, float], coords: dict) -> str | None:
    candidatos = [
        (distancia_metros(posicao, tuple(coord)), nome)
        for nome, coord in coords.get("parks", {}).items()
    ]
    if not candidatos:
        return None
    distancia, nome = min(candidatos)
    return nome if distancia <= RAIO_PARQUE_METROS else None


def coordenada_atracao(do_parque: dict, nome: str):
    """Resolve nome exato e o nome-base antes de um subtítulo separado por hífen.

    Queue-Times às vezes expande o nome sem mudar a atração, como
    ``Expedition Everest - Legend of the Forbidden Mountain``. O coords.json
    conserva o nome curto do OSM. Só removemos subtítulo quando o nome curto
    existe exatamente, evitando casamento parcial ambíguo.
    """
    coord = do_parque.get(nome)
    if coord is not None:
        return coord
    nome_base = nome.split(" - ", 1)[0].strip()
    return do_parque.get(nome_base) if nome_base != nome else None


def _ranking_por_tempo_total(posicao, park_name, payload, config, coords, conn=None):
    """(total, fila, caminhada, metros, atração, coord) ordenado por tempo total.

    O critério é fila + caminhada, não menor fila: 19 min de fila a 7 min de
    caminhada ganha de 16 min de fila a 13 min de caminhada.
    """
    do_parque = coords.get("rides", {}).get(park_name, {})
    limite_obsoleto = config.get("alert", {}).get("max_staleness_minutes", monitor.OBSOLETO_MINUTOS_PADRAO)
    park_cfg = config["parks"].get(park_name, {})

    itens = []
    for _land, ride in monitor.iter_rides(payload):
        nome = ride["name"]
        if monitor.fila_paralela(nome) or not ride.get("is_open"):
            continue
        if monitor.leitura_obsoleta(ride, limite_obsoleto):
            continue
        fila = ride.get("wait_time")
        if fila is None or monitor.get_threshold(park_cfg, nome) is None:
            continue
        coord = coordenada_atracao(do_parque, nome)
        if coord is None:  # sem coordenada entra no fim, sem estimativa
            itens.append((None, fila, None, None, nome, None))
            continue
        metros = distancia_metros(posicao, tuple(coord))
        caminhada = minutos_a_pe(metros)
        itens.append((fila + caminhada, fila, caminhada, metros, nome, tuple(coord)))

    com_coord = sorted([i for i in itens if i[0] is not None])
    destinos = [(item[4], item[5]) for item in com_coord]
    rotas = rotas_google(posicao, destinos)
    if rotas:
        atualizados = []
        for item in com_coord:
            total, fila, caminhada, metros, nome, coord = item
            if nome in rotas:
                caminhada, metros = rotas[nome]
                total = fila + caminhada
            atualizados.append((total, fila, caminhada, metros, nome, coord))
        com_coord = sorted(atualizados)
    sem_coord = sorted([i for i in itens if i[0] is None], key=lambda i: i[1])
    return com_coord + sem_coord, len(rotas), len(destinos)


def ranking_por_tempo_total(posicao, park_name, payload, config, coords, conn=None):
    """Ranking público; detalhes da origem dos tempos ficam para a mensagem."""
    ranking, _rotas, _destinos = _ranking_por_tempo_total(
        posicao, park_name, payload, config, coords, conn)
    return ranking


def com_score(ranking: list, park_name: str, config: dict, conn=None) -> list:
    """Anexa o score a cada item do ranking, mantendo a ordem por tempo total.

    A ordem continua sendo tempo total, que é verificável. O score entra como
    informação a mais, não como critério — se ele discordasse da ordem, a lista
    ficaria confusa justamente no momento em que se precisa decidir rápido.
    """
    com_tempo = [i for i in ranking if i[0] is not None]
    if not com_tempo:
        return [(item, None) for item in ranking]

    pesos = {**PESOS_PADRAO, **config.get("score_weights", {})}
    melhor, pior = com_tempo[0][0], com_tempo[-1][0]
    saida = []
    for item in ranking:
        total, fila, _caminhada, _metros, nome, _coord = item
        if total is None:
            saida.append((item, None))
            continue
        desvio = desvio_da_media(conn, park_name, nome, fila)
        resultado = monitor.tendencia(conn, park_name, nome) if conn is not None else None
        seta = resultado[0] if resultado else None
        saida.append((item, score_oportunidade(total, melhor, pior, desvio, seta, pesos)))
    return saida


def format_perto(posicao, park_name, payload, config, coords, conn=None, limite=5) -> str:
    ranking, rotas_usadas, destinos = _ranking_por_tempo_total(
        posicao, park_name, payload, config, coords, conn)
    if not ranking:
        return (f"📍 <b>{notifier.esc(park_name)}</b>\n\n"
                "Nenhuma atração da watchlist aberta com dado agora.")

    linhas = [
        f"📍 Você está em <b>{notifier.esc(park_name)}</b>",
        f"🕒 {monitor.now_park(config).strftime('%Hh%M')} no horário do parque",
        "",
        "Ordenado por <b>fila + caminhada</b>:",
        "",
    ]
    medalhas = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")
    pontuado = com_score(ranking, park_name, config, conn)
    for i, (item, score) in enumerate(pontuado[:limite]):
        total, fila, caminhada, metros, nome, _coord = item
        medalha = medalhas[i] if i < len(medalhas) else "•"
        seta = monitor.marca_tendencia(conn, park_name, nome)
        if total is None:
            linhas.append(f"{medalha} <b>{notifier.esc(nome)}</b> — fila {fila} min{seta}")
            linhas.append("     <i>sem coordenada: distância desconhecida</i>")
            continue
        estrela = f" · ⭐ {score}" if score is not None else ""
        linhas.append(f"{medalha} <b>{notifier.esc(nome)}</b> — <b>{total} min</b> no total{estrela}")
        linhas.append(f"     fila {fila} min{seta} · 🚶 {caminhada} min ({metros:.0f} m)")

    melhor = ranking[0]
    if melhor[5] is not None:
        rota = MAPS_URL.format(o_lat=posicao[0], o_lon=posicao[1],
                               d_lat=melhor[5][0], d_lon=melhor[5][1])
        linhas += ["", f'🗺️ <a href="{rota}">Abrir rota até {notifier.esc(melhor[4])}</a>']
    if destinos and rotas_usadas == destinos:
        aviso = "Caminhada calculada por rota a pé; confirme o caminho no mapa."
    elif rotas_usadas:
        aviso = (f"Rota a pé disponível para {rotas_usadas} de {destinos} atrações; "
                 "as demais usam estimativa por distância.")
    else:
        aviso = "Caminhada é estimativa por distância, não rota."
    linhas += ["", aviso,
               "Powered by Queue-Times.com"]
    return "\n".join(linhas)
