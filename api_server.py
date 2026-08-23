"""API HTTP privada para o site móvel reutilizar a inteligência do Telegram."""
import hmac
import json
import logging
import math
import os
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
    payload = monitor.fetch_queue_times(park_ids[park_name])
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
    return {"park": park_name, "items": items, "source": "fila-disney-vps"}


class Handler(BaseHTTPRequestHandler):
    # O padrão anuncia "FilaDisneyAPI/1.0 Python/3.12.14" — versão exata do
    # interpretador de graça para quem escaneia. Aqui o banner é só o nome.
    server_version = "FilaDisneyAPI"
    sys_version = ""
    somente_cabecalho = False

    def version_string(self) -> str:
        return self.server_version

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
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
        if parsed.path != "/perto":
            return self._send(404, {"error": "rota não encontrada"})
        agora = time.monotonic()
        if bloqueado(agora):
            return self._send(429, {"error": "muitas tentativas; tente mais tarde"})
        if not token_valido(self.headers.get("Authorization", "")):
            if registrar_falha(agora):
                log.warning("Bloqueando /perto por %ds: %d falhas de token na janela",
                            BLOQUEIO_S, FALHAS_MAX)
            return self._send(401, {"error": "não autorizado"})
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
        log.info("%s - %s", self.address_string(), fmt % args)


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
