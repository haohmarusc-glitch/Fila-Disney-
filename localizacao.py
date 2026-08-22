"""Localização, rota e ranking por tempo total.

Separado do monitor porque é o único bloco que fala de geografia: coordenada,
distância, caminhada e score. O monitor não importa nada daqui em runtime — se
o coords.json não existir, tudo aqui degrada para "sem estimativa" e o resto do
bot funciona igual.

Nenhuma chamada a serviço externo acontece aqui. As coordenadas vêm do
coords.json, gerado uma vez pelo coords.py; a Overpass não é dependência de
execução, só de geração.
"""
import datetime as dt
import json
import logging
import math
import os
import time
from zoneinfo import ZoneInfo

import requests

import monitor
import notifier

log = logging.getLogger("localizacao")

VELOCIDADE_M_POR_MIN = 84      # 5 km/h
FATOR_CAMINHO = 1.3            # linha reta vira caminho real dentro do parque
RAIO_PARQUE_METROS = 2500      # além disso você não está nesse parque
MARGEM_CONTORNO_PARQUE_METROS = 250
MAPS_URL = ("https://www.google.com/maps/dir/?api=1"
            "&origin={o_lat},{o_lon}&destination={d_lat},{d_lon}&travelmode=walking")
GOOGLE_ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
ROTA_CACHE_TTL = 300
# Nao e um teto absoluto: em trajetos curtos, a folga prevalece para tolerar
# entradas deslocadas. IOA usa folga menor por causa dos contornos externos.
ROTA_VELOCIDADE_MIN_M_POR_MIN = 40  # abaixo de 2,4 km/h ja inclui paradas generosas
ROTA_FOLGA_DURACAO_MIN = 5
ROTA_REGRA_PADRAO = {"fator": 3.0, "folga_metros": 500, "teto_metros": 3_000}
ROTA_REGRAS_POR_PARQUE = {
    "Disney Magic Kingdom": {"fator": 3.0, "folga_metros": 500, "teto_metros": 2_500},
    "Epcot": {"fator": 3.0, "folga_metros": 500, "teto_metros": 4_000},
    "Disney Hollywood Studios": {"fator": 3.0, "folga_metros": 500, "teto_metros": 2_000},
    "Disney Animal Kingdom": {"fator": 3.0, "folga_metros": 500, "teto_metros": 3_000},
    "Universal Studios At Universal Orlando": {
        "fator": 3.0, "folga_metros": 350, "teto_metros": 1_800},
    "Islands Of Adventure At Universal Orlando": {
        "fator": 3.0, "folga_metros": 250, "teto_metros": 1_400},
    "Universal Epic Universe": {"fator": 3.0, "folga_metros": 500, "teto_metros": 2_500},
}
_rota_cache = {}

# O score mede somente a qualidade da fila. A posicao decide a medalha por
# fila+caminhada, mas nao pode mudar a nota da mesma fila e tendencia.
PESOS_QUALIDADE_FILA = {"historico": 0.6, "tendencia": 0.4}
MIN_AMOSTRAS_FAIXA = 12
PERFIL_LOOKBACK_DIAS = 56
USF = "Universal Studios At Universal Orlando"
IOA = "Islands Of Adventure At Universal Orlando"
PARK_TO_PARK_PADRAO = {
    "enabled": False,
    "parks": {USF: IOA, IOA: USF},
    "min_savings_minutes": 15,
    "train_ride_minutes": 4,
    "boarding_buffer_minutes": 4,
}


def load_coords() -> dict:
    """Combina o versionado com o volume; o volume vence sem ocultar âncoras."""
    saida = {"parks": {}, "rides": {}}
    for caminho in (monitor.COORDS_PATH_REPO, monitor.COORDS_PATH):
        try:
            with open(caminho, encoding="utf-8") as f:
                dados = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        saida = _mesclar_dict(saida, dados)
    return saida


def _mesclar_dict(base: dict, sobrepor: dict) -> dict:
    """Merge recursivo usado para preservar novas seções do arquivo versionado."""
    saida = dict(base)
    for chave, valor in sobrepor.items():
        if isinstance(valor, dict) and isinstance(saida.get(chave), dict):
            saida[chave] = _mesclar_dict(saida[chave], valor)
        else:
            saida[chave] = valor
    return saida


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


def _chave_cache(posicao, park_name, destinos):
    origem = (round(posicao[0], 3), round(posicao[1], 3))
    return park_name, origem, tuple((nome, tuple(coord)) for nome, coord in destinos)


def validar_rota_caminhada(posicao, destino, metros_rota, minutos_rota,
                           park_name):
    """Devolve ``(valida, motivo)`` antes de uma rota afetar o ranking.

    O fator de 3x e apenas uma referencia: para trajetos curtos, a folga
    configurada evita rejeitar uma entrada deslocada. Em todos os casos, o teto
    proprio do parque e o limite de duracao continuam obrigatorios.
    """
    if metros_rota < 0 or minutos_rota < 1:
        return False, "valor_negativo_ou_zero"
    direta = distancia_metros(posicao, destino)
    regra = {**ROTA_REGRA_PADRAO, **ROTA_REGRAS_POR_PARQUE.get(park_name, {})}
    limite_geometrico = max(
        direta * regra["fator"],
        direta + regra["folga_metros"],
    )
    if metros_rota > min(limite_geometrico, regra["teto_metros"]):
        return False, "distancia_implausivel"
    limite_duracao = math.ceil(metros_rota / ROTA_VELOCIDADE_MIN_M_POR_MIN)
    limite_duracao += ROTA_FOLGA_DURACAO_MIN
    if minutos_rota > limite_duracao:
        return False, "duracao_implausivel"
    return True, "ok"


def registrar_rota_rejeitada(conn, park_name, ride_name, direta, metros,
                             minutos, motivo):
    if conn is None:
        return
    conn.execute(
        "INSERT INTO route_rejections "
        "(ts, park, ride, direct_meters, route_meters, route_minutes, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (monitor.utc_now().isoformat(), park_name, ride_name, direta,
         metros, minutos, motivo),
    )
    conn.commit()


def rotas_google(posicao, park_name, destinos, conn=None):
    """Retorna rotas plausiveis; cada elemento ruim conserva seu fallback."""
    if not GOOGLE_MAPS_API_KEY or not destinos:
        return {}
    chave = _chave_cache(posicao, park_name, destinos)
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
        "X-Goog-FieldMask": (
            "originIndex,destinationIndex,duration,distanceMeters,status,condition"
        ),
    }
    try:
        resposta = monitor.post_json_body(
            GOOGLE_ROUTES_URL, corpo, cabecalhos=headers, tentativas=2)
    except requests.RequestException as exc:
        log.warning("Routes API indisponivel; usando estimativa: %s", exc)
        return {}
    if not isinstance(resposta, list):
        log.warning("Routes API devolveu formato inesperado: %s",
                    type(resposta).__name__)
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
        nome, destino = destinos[indice]
        direta = distancia_metros(posicao, destino)
        valida, motivo = validar_rota_caminhada(
            posicao, destino, metros, minutos, park_name)
        if not valida:
            registrar_rota_rejeitada(
                conn, park_name, nome, direta, metros, minutos, motivo)
            log.warning(
                "Routes API descartada para %s/%s (%s): rota=%sm/%smin, direta=%.0fm; "
                "usando estimativa interna",
                park_name, nome, motivo, metros, minutos, direta,
            )
            continue
        saida[nome] = (minutos, metros)
    _rota_cache[chave] = (agora, saida)
    return saida


def score_qualidade_fila(desvio_historico: float | None, seta: str | None,
                         pesos: dict) -> int:
    """0 a 100 para a fila, independente da posicao do visitante.

    Historico e tendencia sao normalizados; pesos informados sao renormalizados
    para impedir que configuracoes antigas com ``tempo`` distorcam a escala.
    """
    if desvio_historico is None:
        componente_hist = 0.5
    else:
        componente_hist = min(max(0.5 + desvio_historico / 2, 0.0), 1.0)
    componente_tend = {"↓": 1.0, "→": 0.5, "↑": 0.0}.get(seta, 0.5)
    peso_hist = max(0, pesos.get("historico", 0.6))
    peso_tend = max(0, pesos.get("tendencia", 0.4))
    soma = peso_hist + peso_tend or 1
    bruto = (peso_hist * componente_hist + peso_tend * componente_tend) / soma
    return round(bruto * 100)


def percentil(valores: list[int], proporcao: float) -> float:
    """Percentil linear, sem dependência externa; `valores` pode vir desordenado."""
    ordenados = sorted(valores)
    posicao = (len(ordenados) - 1) * proporcao
    inferior = math.floor(posicao)
    superior = math.ceil(posicao)
    if inferior == superior:
        return float(ordenados[inferior])
    peso = posicao - inferior
    return ordenados[inferior] * (1 - peso) + ordenados[superior] * peso


def perfil_historico(conn, config: dict, park: str, ride: str,
                     fila_agora: int, agora=None) -> dict | None:
    """Percentis da mesma atração, hora local e dia da semana.

    O SQLite guarda UTC sem offset. Cada timestamp é convertido para o fuso do
    parque antes do filtro, inclusive através de mudanças entre EST e EDT.
    Retorna None com menos de uma hora de amostras (12 ciclos de cinco minutos).
    """
    if conn is None:
        return None
    momento = agora or monitor.now_park(config)
    fuso = ZoneInfo(config.get("trip", {}).get("timezone", "America/New_York"))
    corte = (momento.astimezone(dt.timezone.utc).replace(tzinfo=None)
             - dt.timedelta(days=PERFIL_LOOKBACK_DIAS)).isoformat()
    def filtrar(rows):
        valores = []
        for timestamp, espera in rows:
            try:
                instante = dt.datetime.fromisoformat(timestamp)
                if instante.tzinfo is None:
                    instante = instante.replace(tzinfo=dt.timezone.utc)
                local = instante.astimezone(fuso)
            except (TypeError, ValueError):
                continue
            if local.weekday() == momento.weekday() and local.hour == momento.hour:
                valores.append(int(espera))
        return valores

    base_sql = (
        "SELECT ts, wait_time FROM wait_times "
        "WHERE park = ? AND ride = ? AND is_open = 1 AND wait_time IS NOT NULL"
    )
    rows = conn.execute(base_sql + " AND ts >= ?", (park, ride, corte)).fetchall()
    valores = filtrar(rows)
    # Prefere uma janela recente para acompanhar a sazonalidade, mas não joga
    # fora uma base madura quando ainda faltam 12 amostras recentes no balde.
    if len(valores) < MIN_AMOSTRAS_FAIXA:
        valores = filtrar(conn.execute(base_sql, (park, ride)).fetchall())
    if len(valores) < MIN_AMOSTRAS_FAIXA:
        return None

    abaixo_ou_igual = sum(valor <= fila_agora for valor in valores)
    posicao = abaixo_ou_igual / len(valores)
    oportunidade = max(min(1 - 2 * posicao, 1.0), -1.0)
    return {
        "p25": percentil(valores, 0.25),
        "mediana": percentil(valores, 0.50),
        "p75": percentil(valores, 0.75),
        "p90": percentil(valores, 0.90),
        "n": len(valores),
        "oportunidade": oportunidade,
    }


def classificar_fila(fila: int, perfil: dict | None) -> str | None:
    if perfil is None:
        return None
    if fila <= perfil["p25"]:
        return "🟢 pequena para este horário"
    if fila <= perfil["mediana"]:
        return "🟡 abaixo do normal"
    if fila <= perfil["p75"]:
        return "🟠 acima do normal"
    if fila <= perfil["p90"]:
        return "🔴 grande para este horário"
    return "🔥 excepcionalmente grande"


def desvio_da_media(conn, park: str, ride: str, fila_agora: int,
                    config: dict | None = None) -> float | None:
    """Compatibilidade: agora mede posição nos percentis da faixa horária."""
    config = config or monitor.load_config()
    perfil = perfil_historico(conn, config, park, ride, fila_agora)
    return perfil["oportunidade"] if perfil else None


def parque_mais_proximo(posicao: tuple[float, float], coords: dict) -> str | None:
    """Identifica parque por contorno das atrações, com margem curta de GPS.

    Um raio de 2,5 km aceitava Yacht Club, Pop Century e parques aquáticos.
    O casco convexo mantém a separação entre USF/IOA e a margem cobre entradas
    e bordas não representadas pelas atrações da watchlist.
    """
    candidatos = []
    for parque, atracoes in coords.get("rides", {}).items():
        pontos = [tuple(coord) for coord in atracoes.values()]
        if not pontos:
            continue
        casco = _casco_convexo(pontos)
        distancia = _distancia_ao_poligono(posicao, casco)
        if distancia <= MARGEM_CONTORNO_PARQUE_METROS:
            candidatos.append((distancia, parque))
    return min(candidatos)[1] if candidatos else None


def _casco_convexo(pontos):
    """Casco convexo monotônico; coordenadas são pequenas o bastante para ordenar em graus."""
    unicos = sorted({(p[1], p[0]) for p in pontos})  # x=lon, y=lat
    if len(unicos) <= 2:
        return [(y, x) for x, y in unicos]

    def cruz(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    baixo = []
    for p in unicos:
        while len(baixo) >= 2 and cruz(baixo[-2], baixo[-1], p) <= 0:
            baixo.pop()
        baixo.append(p)
    alto = []
    for p in reversed(unicos):
        while len(alto) >= 2 and cruz(alto[-2], alto[-1], p) <= 0:
            alto.pop()
        alto.append(p)
    return [(y, x) for x, y in baixo[:-1] + alto[:-1]]


def _dentro_poligono(ponto, poligono):
    if len(poligono) < 3:
        return False
    lat, lon = ponto
    dentro = False
    j = len(poligono) - 1
    for i, (lat_i, lon_i) in enumerate(poligono):
        lat_j, lon_j = poligono[j]
        cruza = ((lat_i > lat) != (lat_j > lat)) and (
            lon < (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i) + lon_i
        )
        if cruza:
            dentro = not dentro
        j = i
    return dentro


def _distancia_segmento_metros(ponto, a, b):
    """Projeção local equiretangular suficiente para segmentos dentro de parque."""
    lat0 = math.radians(ponto[0])
    escala_x = 111_320 * math.cos(lat0)
    escala_y = 110_540
    ax, ay = (a[1] - ponto[1]) * escala_x, (a[0] - ponto[0]) * escala_y
    bx, by = (b[1] - ponto[1]) * escala_x, (b[0] - ponto[0]) * escala_y
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(ax, ay)
    t = max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def _distancia_ao_poligono(ponto, poligono):
    if _dentro_poligono(ponto, poligono):
        return 0.0
    if len(poligono) == 1:
        return distancia_metros(ponto, poligono[0])
    return min(_distancia_segmento_metros(ponto, poligono[i],
                                          poligono[(i + 1) % len(poligono)])
               for i in range(len(poligono)))


def coordenada_atracao(do_parque: dict, nome: str):
    """Resolve o nome canônico e, com segurança, um subtítulo após hífen."""
    coord = do_parque.get(nome)
    if coord is not None:
        return coord
    nome_base = nome.split(" - ", 1)[0].strip()
    return do_parque.get(nome_base) if nome_base != nome else None


def ancora_rota(coords: dict, park_name: str, nome: str, coord_padrao):
    """Ponto caminhável e ajuste final sem apagar a coordenada real."""
    dados = coords.get("route_anchors", {}).get(park_name, {}).get(nome)
    if not isinstance(dados, dict):
        return tuple(coord_padrao), 0, 0
    coord = dados.get("coord", coord_padrao)
    try:
        extra_minutos = max(0, int(dados.get("extra_minutes", 0)))
        extra_metros = max(0, int(dados.get("extra_meters", 0)))
        return tuple(coord), extra_minutos, extra_metros
    except (TypeError, ValueError):
        return tuple(coord_padrao), 0, 0


def _ranking_detalhado(posicao, park_name, payload, config, coords, conn=None):
    """Itens com origem ``google``, ``estimativa`` ou ``sem_coordenada``.

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
        nome_configurado = monitor.nome_watchlist(park_cfg, nome)
        if fila is None or nome_configurado is None:
            continue
        coord = coordenada_atracao(do_parque, nome_configurado)
        if coord is None:  # sem coordenada entra no fim, sem estimativa
            itens.append((None, fila, None, None, nome, None,
                          "sem_coordenada", None))
            continue
        ancora, extra_minutos, extra_metros = ancora_rota(
            coords, park_name, nome_configurado, coord)
        metros_ate_ancora = distancia_metros(posicao, ancora)
        metros = metros_ate_ancora + extra_metros
        caminhada = minutos_a_pe(metros_ate_ancora) + extra_minutos
        itens.append((fila + caminhada, fila, caminhada, metros, nome,
                      tuple(coord), "estimativa", ancora))

    com_coord = sorted([i for i in itens if i[0] is not None])
    destinos = [(item[4], item[7]) for item in com_coord]
    rotas = rotas_google(posicao, park_name, destinos, conn)
    if rotas:
        atualizados = []
        for item in com_coord:
            total, fila, caminhada, metros, nome, coord, origem, ancora = item
            if nome in rotas:
                caminhada, metros = rotas[nome]
                nome_configurado = monitor.nome_watchlist(park_cfg, nome)
                _ancora, extra_minutos, extra_metros = ancora_rota(
                    coords, park_name, nome_configurado, coord)
                caminhada += extra_minutos
                metros += extra_metros
                total = fila + caminhada
                origem = "google"
            atualizados.append((total, fila, caminhada, metros, nome, coord,
                                origem, ancora))
        com_coord = sorted(atualizados)
    sem_coord = sorted([i for i in itens if i[0] is None], key=lambda i: i[1])
    return com_coord + sem_coord


def ranking_por_tempo_total(posicao, park_name, payload, config, coords, conn=None):
    """Interface compativel: tuplas de seis campos, ordenadas por tempo total."""
    return [item[:6] for item in _ranking_detalhado(
        posicao, park_name, payload, config, coords, conn)]


def fila_hogwarts(payload: dict, limite_obsoleto: int) -> int | None:
    for _land, ride in monitor.iter_rides(payload):
        nome = ride.get("name", "").lower().replace("™", "").replace("®", "")
        if "hogwarts express" not in nome or "station" not in nome:
            continue
        if not ride.get("is_open") or monitor.leitura_obsoleta(ride, limite_obsoleto):
            continue
        fila = ride.get("wait_time")
        if isinstance(fila, int) and fila >= 0:
            return fila
    return None


def config_park_to_park(config: dict) -> dict:
    informado = config.get("park_to_park", {})
    saida = {**PARK_TO_PARK_PADRAO, **informado}
    saida["parks"] = {**PARK_TO_PARK_PADRAO["parks"], **informado.get("parks", {})}
    return saida


def avaliar_troca_park_to_park(posicao, park_name, payload_atual, payload_outro,
                               config, coords, conn=None):
    """Sugere o outro parque somente com ingresso habilitado e economia verificável."""
    cfg = config_park_to_park(config)
    outro_parque = cfg.get("parks", {}).get(park_name) if cfg.get("enabled") else None
    if not outro_parque:
        return None
    estacoes = coords.get("park_to_park", {}).get("stations", {})
    partida, chegada = estacoes.get(park_name), estacoes.get(outro_parque)
    if not partida or not chegada:
        return None
    limite = config.get("alert", {}).get(
        "max_staleness_minutes", monitor.OBSOLETO_MINUTOS_PADRAO)
    fila_trem = fila_hogwarts(payload_atual, limite)
    if fila_trem is None:
        return None
    atual = next((i for i in ranking_por_tempo_total(
        posicao, park_name, payload_atual, config, coords, conn) if i[0] is not None), None)
    outro = next((i for i in ranking_por_tempo_total(
        tuple(chegada), outro_parque, payload_outro, config, coords, conn) if i[0] is not None), None)
    if atual is None or outro is None:
        return None
    chave = "__hogwarts_station__"
    rota = rotas_google(posicao, park_name, [(chave, tuple(partida))], conn)
    if chave in rota:
        caminhada_estacao, metros_estacao = rota[chave]
    else:
        metros_estacao = distancia_metros(posicao, tuple(partida))
        caminhada_estacao = minutos_a_pe(metros_estacao)
    viagem = max(0, int(cfg.get("train_ride_minutes", 4)))
    embarque = max(0, int(cfg.get("boarding_buffer_minutes", 4)))
    total_outro = caminhada_estacao + fila_trem + viagem + embarque + outro[0]
    economia = atual[0] - total_outro
    if economia < max(0, int(cfg.get("min_savings_minutes", 15))):
        return None
    return {"park": outro_parque, "ride": outro[4], "total": total_outro,
            "ride_wait": outro[1], "walk_to_station": caminhada_estacao,
            "walk_to_ride": outro[2], "station_meters": metros_estacao,
            "train_wait": fila_trem, "train_ride": viagem,
            "boarding_buffer": embarque, "savings": economia}


def com_score(ranking: list, park_name: str, config: dict, conn=None) -> list:
    """Anexa o score a cada item do ranking, mantendo a ordem por tempo total.

    A ordem continua sendo tempo total, que é verificável. O score entra como
    informação a mais, não como critério — se ele discordasse da ordem, a lista
    ficaria confusa justamente no momento em que se precisa decidir rápido.
    """
    com_tempo = [i for i in ranking if i[0] is not None]
    if not com_tempo:
        return [(item, None) for item in ranking]

    pesos = {**PESOS_QUALIDADE_FILA, **config.get("score_weights", {})}
    saida = []
    for item in ranking:
        total, fila, _caminhada, _metros, nome, _coord = item
        if total is None:
            saida.append((item, None))
            continue
        desvio = desvio_da_media(conn, park_name, nome, fila, config)
        resultado = monitor.tendencia(conn, park_name, nome) if conn is not None else None
        seta = resultado[0] if resultado else None
        saida.append((item, score_qualidade_fila(desvio, seta, pesos)))
    return saida


def format_perto(posicao, park_name, payload, config, coords, conn=None, limite=5,
                 troca=None) -> str:
    ranking_detalhado = _ranking_detalhado(
        posicao, park_name, payload, config, coords, conn)
    ranking = [item[:6] for item in ranking_detalhado]
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
    origens = {item[4]: item[6] for item in ranking_detalhado}
    for i, (item, score) in enumerate(pontuado[:limite]):
        total, fila, caminhada, metros, nome, _coord = item
        medalha = medalhas[i] if i < len(medalhas) else "•"
        seta = monitor.marca_tendencia(conn, park_name, nome)
        if total is None:
            linhas.append(f"{medalha} <b>{notifier.esc(nome)}</b> — fila {fila} min{seta}")
            linhas.append("     <i>sem coordenada: distância desconhecida</i>")
            continue
        estrela = f" · qualidade da fila ⭐ {score}" if score is not None else ""
        linhas.append(f"{medalha} <b>{notifier.esc(nome)}</b> — <b>{total} min</b> no total{estrela}")
        perfil = perfil_historico(conn, config, park_name, nome, fila)
        classe = classificar_fila(fila, perfil)
        contexto = f" · {classe}" if classe else ""
        fonte = "rota Google" if origens[nome] == "google" else "estimativa interna"
        linhas.append(
            f"     fila {fila} min{seta}{contexto} · 🚶 {caminhada} min "
            f"({metros:.0f} m, {fonte})")

    melhor = ranking[0]
    if melhor[5] is not None:
        melhor_detalhado = ranking_detalhado[0]
        destino_rota = melhor_detalhado[7] or melhor[5]
        rota = MAPS_URL.format(o_lat=posicao[0], o_lon=posicao[1],
                               d_lat=destino_rota[0], d_lon=destino_rota[1])
        linhas += ["", f'🗺️ <a href="{rota}">Abrir rota até {notifier.esc(melhor[4])}</a>']
    if troca:
        linhas += ["", f"🚂 <b>Vale trocar para {notifier.esc(troca['park'])}</b>",
                   f"<b>{notifier.esc(troca['ride'])}</b> — <b>{troca['total']} min</b> total",
                   (f"     estação {troca['walk_to_station']} min · trem: fila "
                    f"{troca['train_wait']} + viagem {troca['train_ride']} min · "
                    f"atração: caminhada {troca['walk_to_ride']} + fila {troca['ride_wait']} min"),
                   f"     economia estimada: <b>{troca['savings']} min</b>"]
    usadas = sum(item[6] == "google" for item in ranking_detalhado[:limite])
    estimadas = sum(item[6] == "estimativa" for item in ranking_detalhado[:limite])
    aviso = f"Caminhada: {usadas} rota(s) Google · {estimadas} estimativa(s) interna(s)."
    linhas += ["", aviso,
               "Powered by Queue-Times.com"]
    return "\n".join(linhas)
