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
    valores = []
    rows = conn.execute(
        "SELECT ts, wait_time FROM wait_times "
        "WHERE park = ? AND ride = ? AND is_open = 1 AND wait_time IS NOT NULL",
        (park, ride),
    ).fetchall()
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
    if fila < perfil["p75"]:
        return "🟠 acima do normal"
    if fila < perfil["p90"]:
        return "🔴 grande para este horário"
    return "🔥 excepcionalmente grande"


def desvio_da_media(conn, park: str, ride: str, fila_agora: int,
                    config: dict | None = None) -> float | None:
    """Compatibilidade: agora mede posição nos percentis da faixa horária."""
    config = config or monitor.load_config()
    perfil = perfil_historico(conn, config, park, ride, fila_agora)
    return perfil["oportunidade"] if perfil else None


def parque_mais_proximo(posicao: tuple[float, float], coords: dict) -> str | None:
    """Identifica o parque pela atração mais próxima, não pelo centro.

    Centros são ambíguos em complexos vizinhos como Universal Studios e
    Islands of Adventure. O raio continua sendo apenas um porteiro genérico
    para rejeitar posições completamente fora dos parques.
    """
    candidatos = [
        (distancia_metros(posicao, tuple(coord)), parque)
        for parque, atracoes in coords.get("rides", {}).items()
        for coord in atracoes.values()
    ]
    if not candidatos:
        return None
    distancia, parque = min(candidatos)
    return parque if distancia <= RAIO_PARQUE_METROS else None


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
        if fila is None or monitor.get_threshold(park_cfg, nome) is None:
            continue
        coord = do_parque.get(nome)
        if coord is None:  # sem coordenada entra no fim, sem estimativa
            itens.append((None, fila, None, None, nome, None, "sem_coordenada"))
            continue
        metros = distancia_metros(posicao, tuple(coord))
        caminhada = minutos_a_pe(metros)
        itens.append((fila + caminhada, fila, caminhada, metros, nome,
                      tuple(coord), "estimativa"))

    com_coord = sorted([i for i in itens if i[0] is not None])
    destinos = [(item[4], item[5]) for item in com_coord]
    rotas = rotas_google(posicao, park_name, destinos, conn)
    if rotas:
        atualizados = []
        for item in com_coord:
            total, fila, caminhada, metros, nome, coord, origem = item
            if nome in rotas:
                caminhada, metros = rotas[nome]
                total = fila + caminhada
                origem = "google"
            atualizados.append((total, fila, caminhada, metros, nome, coord, origem))
        com_coord = sorted(atualizados)
    sem_coord = sorted([i for i in itens if i[0] is None], key=lambda i: i[1])
    return com_coord + sem_coord


def ranking_por_tempo_total(posicao, park_name, payload, config, coords, conn=None):
    """Interface compativel: tuplas de seis campos, ordenadas por tempo total."""
    return [item[:6] for item in _ranking_detalhado(
        posicao, park_name, payload, config, coords, conn)]


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


def format_perto(posicao, park_name, payload, config, coords, conn=None, limite=5) -> str:
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
        rota = MAPS_URL.format(o_lat=posicao[0], o_lon=posicao[1],
                               d_lat=melhor[5][0], d_lon=melhor[5][1])
        linhas += ["", f'🗺️ <a href="{rota}">Abrir rota até {notifier.esc(melhor[4])}</a>']
    usadas = sum(item[6] == "google" for item in ranking_detalhado[:limite])
    estimadas = sum(item[6] == "estimativa" for item in ranking_detalhado[:limite])
    aviso = f"Caminhada: {usadas} rota(s) Google · {estimadas} estimativa(s) interna(s)."
    linhas += ["", aviso,
               "Powered by Queue-Times.com"]
    return "\n".join(linhas)
