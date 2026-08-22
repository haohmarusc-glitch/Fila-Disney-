"""API HTTP privada para o site móvel reutilizar a inteligência do Telegram."""
import json
import logging
import math
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import localizacao
import monitor

log = logging.getLogger("web_api")
HOST = "0.0.0.0"
PORT = int(os.environ.get("WEB_API_PORT", "8080"))
TOKEN = os.environ.get("WEB_API_TOKEN", "").strip()


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
    server_version = "FilaDisneyAPI/1.0"

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, {"ok": True, "service": "fila-disney-api"})
        if parsed.path != "/perto":
            return self._send(404, {"error": "rota não encontrada"})
        supplied = self.headers.get("Authorization", "")
        if not TOKEN or supplied != f"Bearer {TOKEN}":
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
    conn = monitor.init_db()
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
