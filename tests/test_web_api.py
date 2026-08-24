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
    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

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

    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado", return_value=[])
    @patch("api_server.monitor.fetch_queue_times", return_value={"lands": []})
    @patch("api_server.localizacao.parque_mais_proximo", return_value="Epcot")
    def test_payload_carrega_a_atribuicao(self, *_mocks):
        """Regra 2: a atribuição é exigência da API gratuita, não enfeite."""
        result = api_server.build_perto_payload(1, 2, object(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["attribution"], "Powered by Queue-Times.com")

    @patch("api_server.localizacao.parque_mais_proximo", return_value=None)
    def test_recusa_local_fora_dos_parques(self, _park):
        with self.assertRaisesRegex(ValueError, "fora dos parques"):
            api_server.build_perto_payload(0, 0, object(), {}, {}, {})


class TestFreioDoToken(unittest.TestCase):
    """A API tem hostname próprio no Caddy: o token enfrenta a internet inteira."""

    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

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
        api_server.limpar_estado()

    def pedir(self, caminho, metodo="GET", token=None):
        conexao = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        cabecalhos = {"Authorization": token} if token else {}
        conexao.request(metodo, caminho, headers=cabecalhos)
        resposta = conexao.getresponse()
        corpo = resposta.read()
        servidor = resposta.getheader("Server")
        self.ultimo_retry_after = resposta.getheader("Retry-After")
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

    def test_token_certo_tambem_tem_ritmo_maximo(self):
        """Cliente com laço bugado chegava autenticado e passava direto."""
        with patch.object(api_server, "TOKEN", "segredo"):
            for _ in range(api_server.PERTO_MAX_JANELA):
                self.pedir("/perto?lat=28.4&lon=-81.5", token="Bearer segredo")
            status, _corpo, _s = self.pedir("/perto?lat=28.4&lon=-81.5",
                                            token="Bearer segredo")
        self.assertEqual(status, 429)
        self.assertEqual(self.ultimo_retry_after, str(api_server.PERTO_JANELA_S),
                         "429 sem Retry-After não diz ao cliente quando voltar")


class TestRitmoAutenticado(unittest.TestCase):
    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

    def test_deixa_passar_ate_o_limite(self):
        for i in range(api_server.PERTO_MAX_JANELA):
            self.assertFalse(api_server.excedeu_ritmo(1000.0 + i / 100))
        self.assertTrue(api_server.excedeu_ritmo(1000.0))

    def test_janela_desliza(self):
        for i in range(api_server.PERTO_MAX_JANELA):
            api_server.excedeu_ritmo(1000.0 + i / 100)
        self.assertFalse(
            api_server.excedeu_ritmo(1000.0 + api_server.PERTO_JANELA_S + 1),
            "passada a janela, a família volta a usar o site")


class TestCacheDaQueueTimes(unittest.TestCase):
    """Cada /perto era um GET a uma API gratuita que publica a cada ~5 min."""

    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

    def test_segunda_chamada_na_janela_nao_bate_na_api(self):
        with patch("api_server.monitor.fetch_queue_times",
                   return_value={"lands": []}) as fetch:
            api_server.payload_do_parque(5, 1000.0)
            api_server.payload_do_parque(5, 1000.0 + api_server.CACHE_TTL_S - 1)
        self.assertEqual(fetch.call_count, 1)

    def test_cache_expira(self):
        with patch("api_server.monitor.fetch_queue_times",
                   return_value={"lands": []}) as fetch:
            api_server.payload_do_parque(5, 1000.0)
            api_server.payload_do_parque(5, 1000.0 + api_server.CACHE_TTL_S + 1)
        self.assertEqual(fetch.call_count, 2)

    def test_cache_e_por_parque(self):
        with patch("api_server.monitor.fetch_queue_times",
                   return_value={"lands": []}) as fetch:
            api_server.payload_do_parque(5, 1000.0)
            api_server.payload_do_parque(6, 1000.0)
        self.assertEqual(fetch.call_count, 2, "um parque não pode responder por outro")

    def test_falha_nao_vira_cache(self):
        """Erro momentâneo não pode congelar o /perto por um minuto inteiro."""
        with patch("api_server.monitor.fetch_queue_times",
                   side_effect=OSError("upstream fora")):
            with self.assertRaises(OSError):
                api_server.payload_do_parque(5, 1000.0)
        with patch("api_server.monitor.fetch_queue_times",
                   return_value={"lands": []}) as fetch:
            api_server.payload_do_parque(5, 1000.5)
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestVigiasPayload(unittest.TestCase):
    """O /vigias é o painel do site: leitura pura, sem identidade vazada.

    O frontend mora fora do repositório (hospedagem do domínio custom), então o
    contrato deste payload é a única interface entre os dois — mudá-lo quebra o
    site em silêncio.
    """

    def setUp(self):
        import importlib
        import tempfile
        from pathlib import Path
        for nome in ("monitor", "localizacao"):
            sys.modules.pop(nome, None)
        self.monitor = importlib.import_module("monitor")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.monitor.DB_PATH = Path(tmp.name) / "h.db"
        self.conn = self.monitor.init_db()
        self.addCleanup(self.conn.close)
        self.config = self.monitor.load_config()
        api_server.monitor = self.monitor

    def _vigia(self, chat="4242", limite=40, pct=None):
        self.conn.execute(
            "INSERT INTO fila_watches (chat_id, park, ride, limite_min, "
            "limite_pct, criado_em) VALUES (?, 'Disney Animal Kingdom', "
            "'Expedition Everest', ?, ?, '2026-08-24T12:00:00')",
            (chat, limite, pct))
        self.conn.commit()

    def _fila(self, wait, aberta=1):
        self.conn.execute(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, 'Disney Animal Kingdom', 'L', 'Expedition Everest', ?, ?)",
            (self.monitor.utc_now().isoformat(), wait, aberta))
        self.conn.commit()

    def test_traz_vigia_com_fila_atual_do_banco(self):
        self._vigia(limite=40)
        self._fila(55)
        payload = api_server.build_vigias_payload(self.conn, self.config)
        vigia = payload["vigias"][0]
        self.assertEqual(vigia["fila_agora"], 55)
        self.assertEqual(vigia["alvo_min"], 40)
        self.assertEqual(payload["attribution"], "Powered by Queue-Times.com")

    def test_chat_id_nunca_aparece(self):
        """Na web, identidade é o nome dado — ou o genérico. Nunca o id."""
        self._vigia(chat="998877")
        texto = str(api_server.build_vigias_payload(self.conn, self.config))
        self.assertNotIn("998877", texto)
        self.assertEqual(
            api_server.build_vigias_payload(self.conn, self.config)["vigias"][0]["quem"],
            "familiar")

    def test_nome_registrado_aparece(self):
        self._vigia(chat="4242")
        self.conn.execute(
            "INSERT INTO chat_names (chat_id, nome, updated_at) "
            "VALUES ('4242', 'Ana', '2026-08-24T12:00:00')")
        self.conn.commit()
        payload = api_server.build_vigias_payload(self.conn, self.config)
        self.assertEqual(payload["vigias"][0]["quem"], "Ana")

    def test_sem_leitura_a_fila_e_None_nunca_zero(self):
        """Regra 15 vale na API também."""
        self._vigia()
        vigia = api_server.build_vigias_payload(self.conn, self.config)["vigias"][0]
        self.assertIsNone(vigia["fila_agora"])
        self.assertIsNone(vigia["aberta"])

    def test_modo_pct_sem_historico_nao_inventa_alvo(self):
        """Regra 12: sem perfil não há 'típico', e o alvo fica None."""
        self._vigia(limite=None, pct=50)
        self._fila(30)
        vigia = api_server.build_vigias_payload(self.conn, self.config)["vigias"][0]
        self.assertEqual(vigia["limite_pct"], 50)
        self.assertIsNone(vigia["alvo_min"])
        self.assertIsNone(vigia["tipico_min"])

    def test_sem_vigias_devolve_lista_vazia(self):
        payload = api_server.build_vigias_payload(self.conn, self.config)
        self.assertEqual(payload["vigias"], [])
        self.assertEqual(payload["max_por_chat"], 5)
