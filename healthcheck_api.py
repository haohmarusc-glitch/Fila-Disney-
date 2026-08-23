"""Healthcheck do container da API: o servidor HTTP responde?

Existe porque o `HEALTHCHECK` da imagem olha a última coleta no banco — que é a
saúde do MONITOR. O container da API herdava esse mesmo teste e aparecia como
"healthy" mesmo com o servidor morto, e como "unhealthy" por culpa do monitor
mesmo estando perfeito. Verificado em produção em 23/08/2026.

Sai 0 se o /health responder 200, 1 em qualquer outro caso.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("WEB_API_PORT", "8080")
URL = f"http://127.0.0.1:{PORT}/health"
TIMEOUT = 5


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as resposta:
            if resposta.status != 200:
                print(f"HTTP {resposta.status}")
                return 1
            corpo = json.loads(resposta.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}")
        return 1
    if not corpo.get("ok"):
        print(f"resposta sem ok: {corpo}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
