"""Apoio dos testes: sobe monitor e notifier com um requests falso.

Os testes exercitam o notifier de verdade (esc, is_authorized, format_alert) —
só a camada HTTP é falsa. Assim um bug de escape ou de autorização aparece aqui
em vez de aparecer no Telegram.
"""
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

TOKEN_FAKE = "123:TOKEN-DE-TESTE"
CHAT_FAKE = "4242"


class Resposta:
    def __init__(self, payload=None, status=200, headers=None, texto=""):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = texto

    def json(self):
        if self._payload is _JSON_QUEBRADO:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            erro = _requests.HTTPError(f"HTTP {self.status_code}")
            erro.response = self
            raise erro


_JSON_QUEBRADO = object()


class RequestsFalso(types.ModuleType):
    """Substitui o módulo requests. `roteador` decide a resposta de cada GET."""

    def __init__(self):
        super().__init__("requests")

        class RequestException(Exception):
            pass

        class HTTPError(RequestException):
            pass

        self.RequestException = RequestException
        self.HTTPError = HTTPError
        self.gets = []
        self.posts = []
        self.roteador = lambda url: Resposta({})
        self.roteador_post = lambda url, payload: Resposta({"ok": True})

    def get(self, url, params=None, timeout=None, headers=None):
        self.gets.append(url)
        self.headers_enviados = headers
        # Guardado porque só a URL não bastava: o heartbeat mandava `status`
        # duplicado por 8 dias e todo teste passava, já que a duplicação
        # acontece entre a query da URL e estes params.
        self.params_enviados = params
        return self.roteador(url)

    def post(self, url, json=None, data=None, timeout=None, headers=None):
        self.posts.append(json if json is not None else data)
        self.headers_enviados = headers
        return self.roteador_post(url, json if json is not None else data)


_requests = RequestsFalso()


class BaseTeste(unittest.TestCase):
    """Cada teste ganha monitor/notifier recém-importados e um banco vazio."""

    def setUp(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN_FAKE
        os.environ["TELEGRAM_CHAT_ID"] = CHAT_FAKE
        global _requests
        _requests = RequestsFalso()
        sys.modules["requests"] = _requests
        self.requests = _requests

        for nome in ("notifier", "monitor", "localizacao", "personagens", "coords"):
            sys.modules.pop(nome, None)
        self.notifier = importlib.import_module("notifier")
        self.monitor = importlib.import_module("monitor")
        self.loc = importlib.import_module("localizacao")

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.monitor.DB_PATH = Path(self.tmp.name) / "history.db"
        # sem redirecionar, o teste escreve coords.json dentro do repo e vaza
        # estado para os testes seguintes
        self.monitor.COORDS_PATH = Path(self.tmp.name) / "data" / "coords.json"
        self.monitor.COORDS_PATH_REPO = Path(self.tmp.name) / "coords.json"
        # sem redirecionar, o /status leria o duracoes.json do repo e o teste
        # passaria a depender de dado de produção
        self.monitor.DURACOES_PATH = Path(self.tmp.name) / "duracoes.json"
        self.monitor._dormir = lambda _s: None  # nenhum teste espera de verdade
        self.conn = self.monitor.init_db()
        self.addCleanup(self.conn.close)
        self.config = self.monitor.load_config()

    def enviadas(self):
        return [p["text"] for p in self.requests.posts]

    def gravar(self, park, ride, wait, ts, aberta=True):
        self.conn.execute(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), park, "Land", ride, wait, int(aberta)),
        )
        self.conn.commit()


def payload_parques(nomes_ids):
    return [{"parks": [{"name": n, "id": i} for n, i in nomes_ids.items()]}]


JSON_QUEBRADO = _JSON_QUEBRADO
