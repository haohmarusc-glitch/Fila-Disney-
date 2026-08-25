import http.client
import sqlite3
import threading
import unittest
import sys
from http.server import HTTPServer
from unittest.mock import patch

from tests.apoio import _requests

sys.modules.setdefault("requests", _requests)
import api_server
import monitor


class TestParametrosAPI(unittest.TestCase):
    def test_aceita_coordenada_valida(self):
        self.assertEqual(api_server._number({"lat": ["28.47"]}, "lat", -90, 90), 28.47)

    def test_recusa_ausente_infinito_e_fora_da_faixa(self):
        for query in ({}, {"lat": ["inf"]}, {"lat": ["91"]}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                api_server._number(query, "lat", -90, 90)


def banco(leituras=()):
    """SQLite em memória com o mínimo que o /perto consulta.

    `leituras` é uma lista de (park, ride, wait_time) repetida quantas vezes
    for preciso — o detector de placeholder exige amostra grande, então os
    testes que dependem dele multiplicam a linha.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE wait_times (ts TEXT, park TEXT, land TEXT, "
                 "ride TEXT, wait_time INTEGER, is_open INTEGER)")
    conn.executemany(
        "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
        "VALUES ('2026-08-24T12:00:00+00:00', ?, 'Land', ?, ?, 1)", leituras)
    return conn


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
        result = api_server.build_perto_payload(1, 2, banco(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["park"], "Epcot")
        self.assertEqual(result["items"][0]["total"], 17)
        self.assertEqual(result["items"][0]["route_source"], "google")

    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado", return_value=[])
    @patch("api_server.monitor.fetch_queue_times", return_value={"lands": []})
    @patch("api_server.localizacao.parque_mais_proximo", return_value="Epcot")
    def test_payload_carrega_a_atribuicao(self, *_mocks):
        """Regra 2: a atribuição é exigência da API gratuita, não enfeite."""
        result = api_server.build_perto_payload(1, 2, banco(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["attribution"], "Powered by Queue-Times.com")

    @patch("api_server.localizacao.parque_mais_proximo", return_value=None)
    def test_recusa_local_fora_dos_parques(self, _park):
        with self.assertRaisesRegex(ValueError, "fora dos parques"):
            api_server.build_perto_payload(0, 0, banco(), {}, {}, {})

    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado", return_value=[])
    @patch("api_server.localizacao.parque_mais_proximo", return_value="Epcot")
    def test_conta_abertas_para_o_site_explicar_a_lista_vazia(self, _park, _rank, _score):
        """Ranking vazio tem duas causas e a tela precisa distinguir: parque
        fechado, ou aberto sem nada elegível. Sem este número o site só
        apagava o painel, que foi o que apareceu no celular em 24/08."""
        payload = {"lands": [{"rides": [
            {"name": "Test Track", "is_open": True, "wait_time": 40},
            {"name": "Soarin'", "is_open": False, "wait_time": 0},
            {"name": "Remy's Single Rider", "is_open": True, "wait_time": 0},
        ]}]}
        with patch("api_server.monitor.fetch_queue_times", return_value=payload):
            result = api_server.build_perto_payload(1, 2, banco(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["items"], [])
        # 1: a fechada não conta e a fila paralela não existe para o usuário
        # (regra 10), senão o site diria "parque aberto" com tudo fechado.
        self.assertEqual(result["abertas"], 1)

    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado", return_value=[])
    @patch("api_server.localizacao.parque_mais_proximo", return_value="Epcot")
    def test_parque_fechado_conta_zero_aberta(self, _park, _rank, _score):
        payload = {"lands": [{"rides": [
            {"name": "Test Track", "is_open": False, "wait_time": 0},
            {"name": "Soarin'", "is_open": False, "wait_time": 0},
        ]}]}
        with patch("api_server.monitor.fetch_queue_times", return_value=payload):
            result = api_server.build_perto_payload(1, 2, banco(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["abertas"], 0)

    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado", return_value=[])
    @patch("api_server.localizacao.parque_mais_proximo",
           return_value="Disney Animal Kingdom")
    def test_show_de_fila_zero_nao_conta_como_parque_aberto(self, *_mocks):
        """O caso medido em 24/08 às 19h ET: o Animal Kingdom estava fechado e
        a Queue-Times publicava 4 atrações abertas — Festival of the Lion King,
        Feathered Friends, Finding Nemo e Tree of Life. Leitura fresca, wait 0,
        porque show e marco não têm fila. Sem descontar o placeholder o site
        diria "leituras velhas" num parque simplesmente fechado.
        """
        shows = ["Festival of the Lion King", "Feathered Friends in Flight!",
                 "Finding Nemo: The Big Blue... and Beyond!", "Tree of Life"]
        # Amostra grande e máxima 0: é assim que o histórico denuncia o
        # placeholder, e é o MESMO detector que o /menores usa.
        leituras = [("Disney Animal Kingdom", nome, 0)
                    for nome in shows
                    for _ in range(monitor.MIN_LEITURAS_PLACEHOLDER)]
        payload = {"lands": [{"rides": [
            {"name": nome, "is_open": True, "wait_time": 0} for nome in shows]}]}
        with patch("api_server.monitor.fetch_queue_times", return_value=payload):
            result = api_server.build_perto_payload(
                1, 2, banco(leituras), {}, {"Disney Animal Kingdom": 8}, {})
        self.assertEqual(result["abertas"], 0, "parque fechado tem zero aberta")

    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado", return_value=[])
    @patch("api_server.localizacao.parque_mais_proximo",
           return_value="Disney Animal Kingdom")
    def test_atracao_que_um_dia_publica_fila_volta_a_contar(self, *_mocks):
        """O detector é histórico, não lista de nomes: se o MAX sobe de 0, a
        atração deixa de ser placeholder sozinha."""
        leituras = [("Disney Animal Kingdom", "Festival of the Lion King", 0)
                    for _ in range(monitor.MIN_LEITURAS_PLACEHOLDER)]
        leituras.append(("Disney Animal Kingdom", "Festival of the Lion King", 25))
        payload = {"lands": [{"rides": [
            {"name": "Festival of the Lion King", "is_open": True, "wait_time": 10}]}]}
        with patch("api_server.monitor.fetch_queue_times", return_value=payload):
            result = api_server.build_perto_payload(
                1, 2, banco(leituras), {}, {"Disney Animal Kingdom": 8}, {})
        self.assertEqual(result["abertas"], 1)


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

    def test_rotas_de_comando_exigem_token(self):
        """Elas rodam formatadores e leem o banco: ficar fora da autenticação
        entregaria o histórico da família a quem achasse o hostname."""
        with patch.object(api_server, "TOKEN", "segredo"):
            for rota in ("/comandos", "/comando?cmd=status&parque=Epcot"):
                with self.subTest(rota=rota):
                    self.assertEqual(self.pedir(rota)[0], 401)

    def test_rota_inexistente_continua_404(self):
        with patch.object(api_server, "TOKEN", "segredo"):
            self.assertEqual(
                self.pedir("/comandox", token="Bearer segredo")[0], 404)

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


class TestParquePayload(unittest.TestCase):
    """O /parque é o /status em JSON: consulta sem GPS, para a aba Roteiro."""

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
        self.monitor.DURACOES_PATH = Path(tmp.name) / "duracoes.json"
        import json as _json
        self.monitor.DURACOES_PATH.write_text(_json.dumps({
            "rides": {"Disney Animal Kingdom": {"Kilimanjaro Safaris": 20}},
            "veiculo": {},
        }), encoding="utf-8")
        self.conn = self.monitor.init_db()
        self.addCleanup(self.conn.close)
        self.config = self.monitor.load_config()
        api_server.monitor = self.monitor
        api_server._cache.clear()
        agora = self.monitor.utc_now().isoformat()
        self.payload = {"lands": [{"name": "L", "rides": [
            {"id": 1, "name": "Kilimanjaro Safaris", "is_open": True,
             "wait_time": 20, "last_updated": agora},
            {"id": 2, "name": "Kali River Rapids", "is_open": False,
             "wait_time": None, "last_updated": agora},
            {"id": 3, "name": "Tree of Life", "is_open": True,
             "wait_time": 0, "last_updated": agora},
        ]}]}
        api_server._cache[99] = (1e18, self.payload)  # cache quente: sem rede
        self.park_ids = {"Disney Animal Kingdom": 99, "Epcot": 5}

    def _payload(self, busca="animal"):
        import time as _t
        api_server._cache[99] = (_t.monotonic(), self.payload)
        return api_server.build_parque_payload(busca, self.conn, self.config, self.park_ids)

    def test_resolve_por_pedaco_do_nome(self):
        dados = self._payload("animal")
        self.assertEqual(dados["park"], "Disney Animal Kingdom")
        self.assertEqual(dados["attribution"], "Powered by Queue-Times.com")

    def test_parque_inexistente_e_erro_claro(self):
        with self.assertRaises(ValueError):
            self._payload("shangri-la")

    def test_busca_ambigua_lista_os_candidatos(self):
        self.park_ids["Disney Animal Kingdom 2"] = 100
        with self.assertRaises(ValueError) as ctx:
            self._payload("animal")
        self.assertIn("ambígua", str(ctx.exception))

    def test_so_watchlist_entra_e_com_duracao(self):
        itens = {i["ride"]: i for i in self._payload()["items"]}
        self.assertIn("Kilimanjaro Safaris", itens)
        self.assertNotIn("Tree of Life", itens, "fora da watchlist não polui")
        self.assertEqual(itens["Kilimanjaro Safaris"]["duracao_min"], 20)
        self.assertEqual(itens["Kilimanjaro Safaris"]["wait"], 20)

    def test_fechada_sem_fila_e_None_nunca_zero(self):
        itens = {i["ride"]: i for i in self._payload()["items"]}
        kali = itens["Kali River Rapids"]
        self.assertFalse(kali["aberta"])
        self.assertIsNone(kali["wait"])

    def test_abertas_vem_antes_por_fila(self):
        nomes = [i["ride"] for i in self._payload()["items"]]
        self.assertEqual(nomes[0], "Kilimanjaro Safaris")
        self.assertEqual(nomes[-1], "Kali River Rapids")


class TestComandosDoSite(unittest.TestCase):
    """Os botões do site rodam os MESMOS formatadores do Telegram."""

    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

    def test_teste_alertas_nao_e_exposto(self):
        """É o único dos 12 comandos de parque que ESCREVE: dispara alertas
        para os chats e devolve None. A API é somente leitura por desenho, e
        um botão desses manda mensagem para a família toda por engano."""
        self.assertNotIn("teste_alertas", api_server.COMANDOS_SITE)

    def test_so_expoe_comando_de_leitura(self):
        """A whitelist é fechada: /vigiar, /entrar e /revogar escrevem no banco
        e precisam de um chat de destino que o site não tem."""
        for escrita in ("vigiar", "entrar", "sair", "revogar", "grupo"):
            self.assertNotIn(escrita, api_server.COMANDOS_SITE)

    def test_todo_comando_exposto_tem_rotulo(self):
        """O site desenha os botões a partir do /comandos — entrada sem rótulo
        viraria botão sem texto."""
        for cmd, cfg in api_server.COMANDOS_SITE.items():
            with self.subTest(cmd=cmd):
                self.assertTrue(cfg["rotulo"].strip())
                self.assertIn("payload", cfg)

    def test_comando_desconhecido_e_recusado(self):
        with self.assertRaisesRegex(ValueError, "não disponível"):
            api_server.executar_comando("rm", "Epcot", None, {}, {"Epcot": 5}, {})

    def test_parque_inexistente_e_recusado(self):
        with self.assertRaisesRegex(ValueError, "não encontrado"):
            api_server.executar_comando("status", "Narnia", None, {}, {"Epcot": 5}, {})

    @patch("api_server.monitor.format_menores", return_value="<b>ok</b>")
    @patch("api_server.monitor.fetch_queue_times", return_value={"lands": []})
    def test_devolve_o_texto_do_formatador(self, _fetch, formatador):
        r = api_server.executar_comando("menores", "epcot", None, {}, {"Epcot": 5}, {})
        self.assertEqual(r["texto"], "<b>ok</b>")
        self.assertEqual(r["parque"], "Epcot")
        self.assertEqual(r["comando"], "menores")
        self.assertTrue(formatador.called)

    @patch("api_server.monitor.format_janela", return_value="janela")
    @patch("api_server.monitor.fetch_queue_times")
    def test_comando_de_historico_nao_gasta_chamada_externa(self, fetch, _fmt):
        """/janela, /resumo e /quebras saem do banco. Buscar a Queue-Times para
        eles seria pedido de rede à toa numa API que a família recarrega."""
        api_server.executar_comando("janela", "Epcot", None, {}, {"Epcot": 5}, {})
        self.assertFalse(fetch.called)


class TestParquePayloadCompleto(unittest.TestCase):
    """O /parque separa watchlist, outras atrações e shows."""

    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

    def payload(self):
        return {"lands": [{"rides": [
            {"name": "Test Track", "is_open": True, "wait_time": 40},
            {"name": "Spaceship Earth", "is_open": True, "wait_time": 15},
            {"name": "Awesome Planet", "is_open": True, "wait_time": 0},
            {"name": "Test Track Single Rider", "is_open": True, "wait_time": 0},
        ]}]}

    def montar(self):
        config = {"parks": {"Epcot": {"attractions": {"Test Track": 30}}},
                  "alert": {"max_staleness_minutes": 20}}
        leituras = [("Epcot", "Awesome Planet", 0)
                    for _ in range(monitor.MIN_LEITURAS_PLACEHOLDER)]
        with patch("api_server.monitor.fetch_queue_times", return_value=self.payload()), \
             patch("api_server.monitor.horario_operacao", return_value=None), \
             patch("api_server.monitor.calcular_lotacao", return_value=None):
            return api_server.build_parque_payload(
                "Epcot", banco(leituras), config, {"Epcot": 5})

    def test_watchlist_fica_em_items(self):
        r = self.montar()
        self.assertEqual([i["ride"] for i in r["items"]], ["Test Track"])

    def test_fora_da_watchlist_com_fila_vai_para_outras(self):
        r = self.montar()
        self.assertEqual([i["ride"] for i in r["outras"]], ["Spaceship Earth"])

    def test_placeholder_vai_para_shows_e_sem_o_campo_wait(self):
        """Show sai sem `wait` de propósito: o 0 dele é ausência de medição,
        não fila curta, e um campo numérico convidaria a tela a exibi-lo."""
        r = self.montar()
        self.assertEqual([i["ride"] for i in r["shows"]], ["Awesome Planet"])
        self.assertNotIn("wait", r["shows"][0])
        self.assertTrue(r["shows"][0]["aberta"])

    def test_fila_paralela_nao_aparece_em_lugar_nenhum(self):
        """Regra 10: single rider não entra em nada que o usuário vê."""
        r = self.montar()
        todos = [i["ride"] for i in r["items"] + r["outras"] + r["shows"]]
        self.assertNotIn("Test Track Single Rider", todos)


class TestCoordenadaNoParque(unittest.TestCase):
    """A coordenada que vira link do Google Maps na tela."""

    def setUp(self):
        api_server.limpar_estado()
        self.addCleanup(api_server.limpar_estado)

    def montar(self, coords):
        config = {"parks": {"Epcot": {"attractions": {"Test Track": 30}}},
                  "alert": {"max_staleness_minutes": 20}}
        payload = {"lands": [{"rides": [
            {"name": "Test Track", "is_open": True, "wait_time": 40}]}]}
        with patch("api_server.monitor.fetch_queue_times", return_value=payload), \
             patch("api_server.monitor.horario_operacao", return_value=None), \
             patch("api_server.monitor.calcular_lotacao", return_value=None):
            return api_server.build_parque_payload(
                "Epcot", banco(), config, {"Epcot": 5}, coords)

    def test_atracao_com_coordenada_leva_o_par(self):
        r = self.montar({"rides": {"Epcot": {"Test Track": [28.3747, -81.5494]}}})
        self.assertEqual(r["items"][0]["coordinate"], [28.3747, -81.5494])

    def test_sem_coordenada_o_campo_e_none(self):
        """Regra 12: sem coordenada real a tela não desenha link. Apontar o
        mapa para o centro do parque como se fosse a atração seria inventar."""
        r = self.montar({"rides": {"Epcot": {}}})
        self.assertIsNone(r["items"][0]["coordinate"])

    def test_sem_coords_json_nenhum_o_parque_ainda_responde(self):
        """O coords.json é opcional (só o /perto depende dele)."""
        r = self.montar(None)
        self.assertIsNone(r["items"][0]["coordinate"])
        self.assertEqual(r["items"][0]["wait"], 40)
