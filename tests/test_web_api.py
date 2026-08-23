import http.client
import threading
import unittest
import sys
from http.server import HTTPServer
from unittest.mock import patch

from tests.apoio import _requests

sys.modules.setdefault("requests", _requests)
import api_server


class TestParametrosAPI(unittest.TestCase):
    def test_aceita_coordenada_valida(self):
        self.assertEqual(api_server._number({"lat": ["28.47"]}, "lat", -90, 90), 28.47)

    def test_recusa_ausente_infinito_e_fora_da_faixa(self):
        for query in ({}, {"lat": ["inf"]}, {"lat": ["91"]}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                api_server._number(query, "lat", -90, 90)


class TestPayloadPerto(unittest.TestCase):
    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado")
    @patch("api_server.monitor.fetch_queue_times", return_value={"lands": []})
    @patch("api_server.localizacao.parque_mais_proximo", return_value="Epcot")
    def test_devolve_json_estruturado(self, _park, _fetch, ranking, _score):
        ranking.return_value = [(17, 10, 7, 500.4, "Test Track", (1.0, 2.0), "google", None)]
        result = api_server.build_perto_payload(1, 2, object(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["park"], "Epcot")
        self.assertEqual(result["items"][0]["total"], 17)
        self.assertEqual(result["items"][0]["route_source"], "google")

    @patch("api_server.localizacao.parque_mais_proximo", return_value=None)
    def test_recusa_local_fora_dos_parques(self, _park):
        with self.assertRaisesRegex(ValueError, "fora dos parques"):
            api_server.build_perto_payload(0, 0, object(), {}, {}, {})


class TestFreioDoToken(unittest.TestCase):
    """A API tem hostname próprio no Caddy: o token enfrenta a internet inteira."""

    def setUp(self):
        api_server._falhas.clear()
        api_server._bloqueado_ate = 0.0
        self.addCleanup(api_server._falhas.clear)

    def test_token_certo_e_errado(self):
        with patch.object(api_server, "TOKEN", "segredo"):
            self.assertTrue(api_server.token_valido("Bearer segredo"))
            self.assertFalse(api_server.token_valido("Bearer outro"))
            self.assertFalse(api_server.token_valido("segredo"))
            self.assertFalse(api_server.token_valido(""))

    def test_sem_token_configurado_nada_e_valido(self):
        with patch.object(api_server, "TOKEN", ""):
            self.assertFalse(api_server.token_valido("Bearer "))

    def test_bloqueia_depois_do_limite_de_falhas(self):
        for i in range(api_server.FALHAS_MAX - 1):
            self.assertFalse(api_server.registrar_falha(100.0 + i),
                             "não pode bloquear antes do limite")
            self.assertFalse(api_server.bloqueado(100.0 + i))
        self.assertTrue(api_server.registrar_falha(100.0 + api_server.FALHAS_MAX))
        self.assertTrue(api_server.bloqueado(200.0))

    def test_bloqueio_expira(self):
        for i in range(api_server.FALHAS_MAX):
            api_server.registrar_falha(100.0 + i)
        self.assertTrue(api_server.bloqueado(200.0))
        self.assertFalse(api_server.bloqueado(100.0 + api_server.BLOQUEIO_S + 60))

    def test_falhas_espalhadas_no_tempo_nao_bloqueiam(self):
        for i in range(api_server.FALHAS_MAX * 2):
            momento = i * (api_server.FALHAS_JANELA_S + 1)
            api_server.registrar_falha(momento)
            self.assertFalse(api_server.bloqueado(momento),
                             "erro isolado de familiar não pode derrubar o acesso")



class TestServidorHTTP(unittest.TestCase):
    """Sobe o servidor de verdade: o caminho de auth não tinha teste nenhum.

    O `build` do CI monta a imagem mas não executa `api_server.py`; um erro aqui
    só apareceria com o site fora do ar.
    """

    @classmethod
    def setUpClass(cls):
        cls.servidor = HTTPServer(("127.0.0.1", 0), api_server.Handler)
        cls.servidor.conn = cls.servidor.config = None
        cls.servidor.park_ids = cls.servidor.coords = {}
        cls.porta = cls.servidor.server_address[1]
        cls.thread = threading.Thread(target=cls.servidor.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def setUp(self):
        api_server._falhas.clear()
        api_server._bloqueado_ate = 0.0

    def pedir(self, caminho, metodo="GET", token=None):
        conexao = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        cabecalhos = {"Authorization": token} if token else {}
        conexao.request(metodo, caminho, headers=cabecalhos)
        resposta = conexao.getresponse()
        corpo = resposta.read()
        servidor = resposta.getheader("Server")
        conexao.close()
        return resposta.status, corpo, servidor

    def test_health_e_publico(self):
        status, corpo, _ = self.pedir("/health")
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": true', corpo)

    def test_head_nao_devolve_501(self):
        status, corpo, _ = self.pedir("/health", metodo="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(corpo, b"", "HEAD não pode ter corpo")

    def test_banner_nao_revela_a_versao_do_python(self):
        _status, _corpo, servidor = self.pedir("/health")
        self.assertEqual(servidor, "FilaDisneyAPI")
        self.assertNotIn("Python", servidor)

    def test_perto_sem_token_e_com_token_errado(self):
        with patch.object(api_server, "TOKEN", "segredo"):
            self.assertEqual(self.pedir("/perto?lat=28.4&lon=-81.5")[0], 401)
            self.assertEqual(
                self.pedir("/perto?lat=28.4&lon=-81.5", token="Bearer errado")[0], 401)

    def test_rota_desconhecida(self):
        self.assertEqual(self.pedir("/qualquer")[0], 404)

    def test_chute_de_token_leva_a_429(self):
        with patch.object(api_server, "TOKEN", "segredo"):
            for _ in range(api_server.FALHAS_MAX):
                self.pedir("/perto?lat=28.4&lon=-81.5", token="Bearer errado")
            status, _corpo, _s = self.pedir("/perto?lat=28.4&lon=-81.5", token="Bearer errado")
            self.assertEqual(status, 429, "token sem freio é chutável pela internet")

if __name__ == "__main__":
    unittest.main()
