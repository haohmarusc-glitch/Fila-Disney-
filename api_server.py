"""API HTTP privada para o site móvel reutilizar a inteligência do Telegram."""
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import localizacao
import monitor

log = logging.getLogger("web_api")
HOST = "0.0.0.0"
PORT = int(os.environ.get("WEB_API_PORT", "8080"))
TOKEN = os.environ.get("WEB_API_TOKEN", "").strip()

# O Caddy publica esta API num hostname próprio, então o token é a única
# barreira contra a internet inteira. Sem freio ele é chutável à vontade.
# O limite é global de propósito: atrás do proxy todo cliente chega com o mesmo
# IP, então contar por IP bloquearia todo mundo junto. Cliente legítimo não
# erra token, então na prática só quem está chutando encosta neste limite.
FALHAS_MAX = 10
FALHAS_JANELA_S = 300
BLOQUEIO_S = 300
ESPERA_BANCO_S = 60      # o monitor cria o banco; a API só lê
_falhas: list[float] = []
_bloqueado_ate = 0.0

# Token válido também precisa de freio: o limite acima só segura quem erra o
# token. Um cliente com bug em laço, ou a família toda recarregando junto,
# chegava autenticado e passava direto — cada pedido virando um GET à
# Queue-Times e uma volta no ranking. O limite é global pelo mesmo motivo do
# outro: atrás do Caddy todo mundo tem o mesmo IP.
PERTO_MAX_JANELA = 30
PERTO_JANELA_S = 60
_pedidos: list[float] = []

# A Queue-Times publica em ciclos de ~5 min: dois /perto seguidos devolviam o
# mesmo dado ao custo de duas chamadas a uma API gratuita. 60s é curto o
# bastante para o ranking não envelhecer e longo o bastante para absorver a
# rajada de todo mundo abrindo o site ao mesmo tempo.
CACHE_TTL_S = 60
_cache: dict[int, tuple[float, dict]] = {}


def token_valido(recebido: str) -> bool:
    """Comparação de tempo constante, como já é feito na senha familiar."""
    return bool(TOKEN) and hmac.compare_digest(recebido, f"Bearer {TOKEN}")


def bloqueado(agora: float) -> bool:
    return agora < _bloqueado_ate


def registrar_falha(agora: float) -> bool:
    """Anota a falha e devolve True se acabou de acionar o bloqueio."""
    global _bloqueado_ate
    _falhas.append(agora)
    del _falhas[: max(0, len(_falhas) - FALHAS_MAX)]
    recentes = [t for t in _falhas if agora - t <= FALHAS_JANELA_S]
    if len(recentes) >= FALHAS_MAX:
        _bloqueado_ate = agora + BLOQUEIO_S
        _falhas.clear()
        return True
    return False


def excedeu_ritmo(agora: float) -> bool:
    """Freio de pedidos autenticados; anota o pedido quando ele passa."""
    global _pedidos
    _pedidos = [t for t in _pedidos if agora - t < PERTO_JANELA_S]
    if len(_pedidos) >= PERTO_MAX_JANELA:
        return True
    _pedidos.append(agora)
    return False


def payload_do_parque(park_id: int, agora: float) -> dict:
    """Payload da Queue-Times com cache curto por parque."""
    entrada = _cache.get(park_id)
    if entrada is not None and agora - entrada[0] < CACHE_TTL_S:
        return entrada[1]
    # Falha não vira cache: um erro momentâneo não pode congelar por 60s. Sem
    # entrada nova, o pedido seguinte tenta de novo.
    payload = monitor.fetch_queue_times(park_id)
    _cache[park_id] = (agora, payload)
    return payload


def limpar_estado() -> None:
    """Zera cache e contadores — usado pelos testes, que rodam no mesmo processo."""
    global _bloqueado_ate, _pedidos
    _falhas.clear()
    _pedidos = []
    _cache.clear()
    _bloqueado_ate = 0.0


def esperar_banco(espera_s: int = ESPERA_BANCO_S) -> None:
    """Numa VPS nova a API pode subir antes do monitor criar o banco.

    Esperar é melhor que morrer em loop de restart: o arquivo aparece no
    primeiro ciclo do monitor, em segundos.
    """
    limite = time.monotonic() + espera_s
    while not monitor.DB_PATH.exists():
        if time.monotonic() >= limite:
            raise SystemExit(
                f"banco não encontrado em {monitor.DB_PATH} — suba o monitor primeiro")
        time.sleep(2)


def _rotulo_do_chat(conn, chat_id) -> str:
    row = conn.execute(
        "SELECT nome FROM chat_names WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return row[0] if row else "familiar"


def build_vigias_payload(conn, config: dict) -> dict:
    """As vigias de fila ativas, com a fila atual e o alvo — somente leitura.

    O site é o painel; criar e cancelar continua no Telegram, porque o alerta
    precisa de um chat de destino e o site tem um token só para a família toda.
    A identidade de quem vigia sai como o nome do `chat_names` quando existe —
    nunca o chat_id, que não é dado de tela.

    A fila atual vem do BANCO (última leitura do ciclo), não da Queue-Times:
    este endpoint não gasta chamada externa e envelhece no máximo 5 min, que é
    o próprio passo do monitor.
    """
    vigias = []
    rows = conn.execute(
        "SELECT chat_id, park, ride, limite_min, limite_pct, criado_em "
        "FROM fila_watches ORDER BY park, ride").fetchall()
    for chat_id, park, ride, limite_min, limite_pct, criado_em in rows:
        atual = conn.execute(
            "SELECT wait_time, is_open, ts FROM wait_times "
            "WHERE park = ? AND ride = ? ORDER BY ts DESC LIMIT 1",
            (park, ride)).fetchone()
        fila = atual[0] if atual else None
        alvo_min = limite_min
        tipico = None
        if limite_pct is not None and fila is not None:
            perfil = localizacao.perfil_historico(conn, config, park, ride, fila)
            if perfil and perfil.get("mediana"):
                tipico = round(perfil["mediana"])
                alvo_min = round(perfil["mediana"] * limite_pct / 100)
        vigias.append({
            "park": park,
            "ride": ride,
            # nome_do_chat cai em "chat <id>" quando não há rótulo — bom no
            # Telegram, vazamento na web. Aqui: nome ou o genérico "familiar".
            "quem": _rotulo_do_chat(conn, chat_id),
            "limite_min": limite_min,
            "limite_pct": limite_pct,
            "alvo_min": alvo_min,       # None no modo % sem histórico: sem chute
            "tipico_min": tipico,
            "fila_agora": fila,         # None é None — nunca vira 0 (regra 15)
            "aberta": bool(atual[1]) if atual else None,
            "criado_em": criado_em,
        })
    return {"vigias": vigias,
            "max_por_chat": monitor.MAX_VIGIAS_FILA_POR_CHAT,
            "attribution": "Powered by Queue-Times.com"}


def _number(query: dict, key: str, low: float, high: float) -> float:
    try:
        value = float(query[key][0])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"parâmetro {key} inválido") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"parâmetro {key} fora da faixa")
    return value


def build_perto_payload(latitude: float, longitude: float, conn, config, park_ids, coords):
    position = (latitude, longitude)
    park_name = localizacao.parque_mais_proximo(position, coords)
    if park_name is None or park_name not in park_ids:
        raise ValueError("localização fora dos parques monitorados")
    payload = payload_do_parque(park_ids[park_name], time.monotonic())
    detailed = localizacao._ranking_detalhado(position, park_name, payload, config, coords, conn)
    ranking = [item[:6] for item in detailed]
    scores = {item[4]: score for item, score in localizacao.com_score(ranking, park_name, config, conn)}
    items = []
    for total, wait, walk, meters, name, coord, source, _anchor in detailed[:12]:
        items.append({
            "name": name, "wait": wait, "walk": walk,
            "meters": round(meters) if meters is not None else None,
            "total": total, "coordinate": list(coord) if coord else None,
            "route_source": source, "quality": scores.get(name),
        })
    # Regra 2 do projeto e exigência da API gratuita: a atribuição tem que estar
    # visível. Toda mensagem do Telegram já a carrega; o JSON do site não
    # carregava nenhuma, e é a superfície mais visível do projeto. Vai no payload
    # para que a página não dependa de alguém lembrar de escrevê-la no HTML.
    return {"park": park_name, "items": items, "source": "fila-disney-vps",
            "attribution": "Powered by Queue-Times.com"}


# Tira ?a=b&c=d de qualquer lugar da linha, parando no espaço ou nas aspas que
# fecham a request line.
_SEM_QUERY = re.compile(r"\?[^\s\"]*")


class Handler(BaseHTTPRequestHandler):
    # O padrão anuncia "FilaDisneyAPI/1.0 Python/3.12.14" — versão exata do
    # interpretador de graça para quem escaneia. Aqui o banner é só o nome.
    server_version = "FilaDisneyAPI"
    sys_version = ""
    somente_cabecalho = False

    def version_string(self) -> str:
        return self.server_version

    def _send(self, status: int, payload: dict, extras: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        for nome, valor in (extras or {}).items():
            self.send_header(nome, valor)
        self.end_headers()
        if not self.somente_cabecalho:
            self.wfile.write(body)

    def do_HEAD(self):
        """Sem isto o HEAD devolve 501 e quebra monitor que checa com HEAD."""
        self.somente_cabecalho = True
        try:
            self.do_GET()
        finally:
            self.somente_cabecalho = False

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, {"ok": True, "service": "fila-disney-api"})
        if parsed.path not in ("/perto", "/vigias"):
            return self._send(404, {"error": "rota não encontrada"})
        agora = time.monotonic()
        if bloqueado(agora):
            return self._send(429, {"error": "muitas tentativas; tente mais tarde"})
        if not token_valido(self.headers.get("Authorization", "")):
            if registrar_falha(agora):
                log.warning("Bloqueando /perto por %ds: %d falhas de token na janela",
                            BLOQUEIO_S, FALHAS_MAX)
            return self._send(401, {"error": "não autorizado"})
        if excedeu_ritmo(agora):
            log.warning("Ritmo de /perto excedido: mais de %d pedidos em %ds",
                        PERTO_MAX_JANELA, PERTO_JANELA_S)
            return self._send(429, {"error": "muitos pedidos; tente em instantes"},
                              {"Retry-After": str(PERTO_JANELA_S)})
        if parsed.path == "/vigias":
            try:
                return self._send(200, build_vigias_payload(
                    self.server.conn, self.server.config))
            except Exception:
                log.exception("Falha em /vigias")
                return self._send(503, {"error": "vigias temporariamente indisponíveis"})
        try:
            query = parse_qs(parsed.query)
            lat = _number(query, "lat", -90, 90)
            lon = _number(query, "lon", -180, 180)
            result = build_perto_payload(lat, lon, self.server.conn, self.server.config,
                                         self.server.park_ids, self.server.coords)
            return self._send(200, result)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception:
            log.exception("Falha em /perto")
            return self._send(503, {"error": "ranking temporariamente indisponível"})

    def log_message(self, fmt, *args):
        # O formato padrão registra a linha de request inteira, e em /perto ela
        # traz ?lat=&lon= — a posição exata de quem está no parque indo parar no
        # log do Docker, que guarda 30 MB e é legível por quem tiver o servidor.
        # A rota basta para operar; a coordenada não acrescenta nada aqui.
        log.info("%s - %s", self.address_string(), _SEM_QUERY.sub("", fmt % args))


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not TOKEN:
        raise SystemExit("WEB_API_TOKEN ausente")
    config = monitor.load_config()
    esperar_banco()
    # Somente leitura: quem cria e escreve neste banco é o monitor. Garantido
    # pelo SQLite, não pela confiança de que o código daqui não escreve.
    try:
        conn = monitor.conectar_somente_leitura()
    except sqlite3.OperationalError as exc:
        # Em WAL, um leitor precisa do -shm que o escritor mantém. Com o monitor
        # parado a abertura falha — mensagem clara em vez de erro cru, e o
        # restart do compose resolve assim que o monitor voltar.
        raise SystemExit(
            f"não consegui abrir o banco para leitura ({exc}); "
            "o container fila-disney precisa estar rodando"
        ) from exc
    # Uma requisição por vez mantém a conexão SQLite no thread que a criou.
    # Para uso familiar isso também funciona como limite natural de carga.
    server = HTTPServer((HOST, PORT), Handler)
    server.conn, server.config = conn, config
    server.park_ids = monitor.resolve_park_ids(list(config["parks"]))
    server.coords = localizacao.load_coords()
    log.info("API privada ouvindo em %s:%d", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
