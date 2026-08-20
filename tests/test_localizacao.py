"""Obsolescência do dado, distância, e ranking por fila + caminhada."""
import datetime as dt
import unittest

from tests.apoio import CHAT_FAKE, BaseTeste, Resposta

EDT = dt.timezone(dt.timedelta(hours=-4))

# Coordenadas fictícias, só para a geometria: 3 pontos a distâncias conhecidas
# do observador. Nenhuma delas é usada em produção.
PORTAO = (28.4180, -81.5810)
COORDS = {
    "parks": {"Disney Hollywood Studios": [28.4180, -81.5810]},
    "rides": {"Disney Hollywood Studios": {
        "Toy Story Mania!": [28.4189, -81.5810],          # ~100 m
        "Tower of Terror": [28.4225, -81.5810],           # ~500 m
        "Slinky Dog Dash": [28.4270, -81.5810],           # ~1000 m
    }},
}


def ride(nome, fila, aberta=True, atualizado=None):
    r = {"name": nome, "wait_time": fila, "is_open": aberta}
    if atualizado is not None:
        r["last_updated"] = atualizado
    return r


class TestObsolescencia(BaseTeste):
    def agora_iso(self, minutos_atras=0):
        marca = self.monitor.utc_now() - dt.timedelta(minutes=minutos_atras)
        return marca.isoformat() + "Z"

    def test_recente_vale(self):
        self.assertFalse(self.monitor.leitura_obsoleta(ride("X", 10, atualizado=self.agora_iso(3))))

    def test_velha_e_descartada(self):
        self.assertTrue(self.monitor.leitura_obsoleta(ride("X", 10, atualizado=self.agora_iso(180))))

    def test_sem_o_campo_vale_o_dado(self):
        self.assertFalse(self.monitor.leitura_obsoleta({"name": "X", "wait_time": 10}))

    def test_campo_corrompido_vale_o_dado(self):
        self.assertFalse(self.monitor.leitura_obsoleta(ride("X", 10, atualizado="ontem")))

    def test_nao_alerta_com_dado_velho(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 5, atualizado=self.agora_iso(200)),
        ]}]}
        self.requests.roteador = lambda url: Resposta(payload)
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)
        self.monitor.run_cycle(self.conn, self.config, {"Disney Hollywood Studios": 7})
        self.assertEqual(self.enviadas(), [], "5 min de fila com dado de 3h atrás não alerta")
        gravadas = self.conn.execute("SELECT COUNT(*) FROM wait_times").fetchone()[0]
        self.assertEqual(gravadas, 1, "mas o histórico continua gravando")

    def test_status_mostra_como_desatualizada(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 5, atualizado=self.agora_iso(200)),
        ]}]}
        texto = self.monitor.format_status("Disney Hollywood Studios", payload, self.config)
        linha = next(l for l in texto.split("\n") if "Toy Story" in l)
        self.assertIn("desatualizado", linha)
        self.assertNotIn("✅", linha, "não pode parecer oportunidade")
        self.assertIn("⏳", linha)


class TestGeometria(BaseTeste):
    def test_distancia_confere_com_a_referencia(self):
        metros = self.monitor.distancia_metros(PORTAO, (28.4270, -81.5810))
        self.assertAlmostEqual(metros, 1000, delta=15)

    def test_distancia_zero(self):
        self.assertAlmostEqual(self.monitor.distancia_metros(PORTAO, PORTAO), 0, delta=0.1)

    def test_caminhada_cresce_com_a_distancia(self):
        self.assertEqual(self.monitor.minutos_a_pe(0), 1, "mínimo de 1 min")
        self.assertEqual(self.monitor.minutos_a_pe(500), 8)
        self.assertLess(self.monitor.minutos_a_pe(100), self.monitor.minutos_a_pe(1000))

    def test_parque_mais_proximo(self):
        self.assertEqual(
            self.monitor.parque_mais_proximo(PORTAO, COORDS), "Disney Hollywood Studios")

    def test_longe_de_tudo_nao_casa_parque(self):
        sao_paulo = (-23.55, -46.63)
        self.assertIsNone(self.monitor.parque_mais_proximo(sao_paulo, COORDS))


class TestRankingPorTempoTotal(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)

    def ranking(self, payload):
        return self.monitor.ranking_por_tempo_total(
            PORTAO, "Disney Hollywood Studios", payload, self.config, COORDS)

    def test_fila_menor_perde_para_tempo_total_menor(self):
        """O caso que justifica o recurso: 25+2 ganha de 20+11."""
        payload = {"lands": [{"name": "L", "rides": [
            ride("Slinky Dog Dash", 20),        # ~1000 m -> ~11 min a pé = 31
            ride("Toy Story Mania!", 25),       # ~100 m  -> ~2 min a pé  = 27
        ]}]}
        primeiro = self.ranking(payload)[0]
        self.assertEqual(primeiro[4], "Toy Story Mania!")
        self.assertLess(primeiro[0], 31)

    def test_ordena_por_total(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Slinky Dog Dash", 10),
            ride("Tower of Terror", 10),
            ride("Toy Story Mania!", 10),
        ]}]}
        nomes = [i[4] for i in self.ranking(payload)]
        self.assertEqual(nomes, ["Toy Story Mania!", "Tower of Terror", "Slinky Dog Dash"],
                         "fila igual: vence a mais perto")

    def test_sem_coordenada_vai_para_o_fim_sem_estimativa(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Mickey & Minnie's Runaway Railway", 5),   # fora do COORDS
            ride("Tower of Terror", 40),
        ]}]}
        ranking = self.ranking(payload)
        self.assertEqual(ranking[-1][4], "Mickey & Minnie's Runaway Railway")
        self.assertIsNone(ranking[-1][0], "sem coordenada não inventa tempo")

    def test_fechada_e_obsoleta_ficam_fora(self):
        velho = (self.monitor.utc_now() - dt.timedelta(hours=3)).isoformat() + "Z"
        payload = {"lands": [{"name": "L", "rides": [
            ride("Tower of Terror", 10, aberta=False),
            ride("Slinky Dog Dash", 10, atualizado=velho),
            ride("Toy Story Mania!", 30),
        ]}]}
        nomes = [i[4] for i in self.ranking(payload)]
        self.assertEqual(nomes, ["Toy Story Mania!"])


class TestMensagemPerto(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)
        self.payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 25),
            ride("Tower of Terror", 15),
        ]}]}
        self.requests.roteador = lambda url: Resposta(self.payload)
        self.parques = {"Disney Hollywood Studios": 7}

    def test_mensagem_traz_total_caminhada_e_rota(self):
        texto = self.monitor.format_perto(
            PORTAO, "Disney Hollywood Studios", self.payload, self.config, COORDS)
        self.assertIn("no total", texto)
        self.assertIn("🚶", texto)
        self.assertIn("google.com/maps/dir", texto)

    def test_sem_coords_json_orienta_rodar_o_script(self):
        r = self.monitor.responder_localizacao(
            *PORTAO, self.conn, self.config, self.parques, {"parks": {}, "rides": {}})
        self.assertIn("coords.py", r)

    def test_fora_dos_parques_avisa(self):
        r = self.monitor.responder_localizacao(
            -23.55, -46.63, self.conn, self.config, self.parques, COORDS)
        self.assertIn("Não achei nenhum parque", r)

    def test_update_de_localizacao_e_atendido(self):
        self.requests.roteador = lambda url: (
            Resposta({"result": [{"update_id": 7, "message": {
                "chat": {"id": int(CHAT_FAKE)},
                "location": {"latitude": PORTAO[0], "longitude": PORTAO[1]}}}]})
            if "getUpdates" in url else Resposta(self.payload))
        self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0, COORDS)
        self.assertTrue(any("no total" in t for t in self.enviadas()))

    def test_localizacao_de_chat_nao_autorizado_e_ignorada(self):
        self.requests.roteador = lambda url: (
            Resposta({"result": [{"update_id": 7, "message": {
                "chat": {"id": 999999},
                "location": {"latitude": PORTAO[0], "longitude": PORTAO[1]}}}]})
            if "getUpdates" in url else Resposta(self.payload))
        self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0, COORDS)
        self.assertEqual(self.enviadas(), [])

    def test_comando_perto_pede_localizacao(self):
        r = self.monitor.handle_command("/perto", self.conn, self.config, self.parques)
        self.assertIs(r, self.monitor.PEDIR_LOCALIZACAO)
        self.assertIs(self.monitor.handle_command(
            "/agora", self.conn, self.config, self.parques), self.monitor.PEDIR_LOCALIZACAO)


class TestCasamentoDeNomes(BaseTeste):
    """coords.py não roda aqui (precisa da Overpass), mas o matching sim."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")

    def test_normaliza_acento_pontuacao_e_patrocinio(self):
        self.assertEqual(
            self.coords.normalizar("Rock 'n' Roller Coaster Starring Aerosmith"),
            self.coords.normalizar("Rock n Roller Coaster"))
        self.assertEqual(
            self.coords.normalizar("Test Track Presented by Chevrolet"),
            self.coords.normalizar("Test Track"))

    def test_casa_exato_e_parcial(self):
        osm = {"tower of terror": (1.0, 2.0), "slinky dog dash": (3.0, 4.0)}
        self.assertEqual(self.coords.casar("Slinky Dog Dash", osm)[0], "slinky dog dash")
        casado, confianca = self.coords.casar("The Twilight Zone Tower of Terror", osm)
        self.assertEqual(casado, "tower of terror")
        self.assertGreater(confianca, 0.75, "substring do nome inteiro é match forte")

    def test_nao_casa_o_que_nao_existe(self):
        osm = {"tower of terror": (1.0, 2.0)}
        self.assertIsNone(self.coords.casar("Space Mountain", osm))

    def test_nao_casa_por_uma_palavra_solta(self):
        osm = {"jungle cruise": (1.0, 2.0)}
        self.assertIsNone(self.coords.casar("Kilimanjaro Safaris Cruise", osm),
                          "uma palavra em comum não é evidência suficiente")


if __name__ == "__main__":
    unittest.main()


class TestSanidadeDasCoordenadas(BaseTeste):
    """O parks.json trouxe Epic Universe com longitude positiva em 20/08/2026."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")
        self.reais = {
            "Disney Magic Kingdom": (28.417663, -81.581212),
            "Epcot": (28.374694, -81.549404),
            "Disney Hollywood Studios": (28.3575294, -81.5582714),
            "Disney Animal Kingdom": (28.3530666, -81.5911943),
            "Universal Studios At Universal Orlando": (28.4749822, -81.466497),
            "Islands Of Adventure At Universal Orlando": (28.472243, -81.4678556),
            "Universal Epic Universe": (28.44144545, 81.44867409),
        }

    def test_isola_o_parque_com_dado_errado(self):
        bons, suspeitos = self.coords.coordenadas_sanas(self.reais)
        self.assertNotIn("Universal Epic Universe", bons)
        self.assertEqual(len(bons), 6, "os outros seis seguem normalmente")
        self.assertEqual(len(suspeitos), 1)

    def test_aponta_o_erro_de_sinal(self):
        _bons, suspeitos = self.coords.coordenadas_sanas(self.reais)
        self.assertIn("erro de sinal", suspeitos[0])
        self.assertIn("-81.44867409", suspeitos[0], "sugere a coordenada corrigida")

    def test_todas_boas_passam_inteiras(self):
        boas = {k: v for k, v in self.reais.items() if k != "Universal Epic Universe"}
        bons, suspeitos = self.coords.coordenadas_sanas(boas)
        self.assertEqual(len(bons), 6)
        self.assertEqual(suspeitos, [])

    def test_poucos_parques_nao_da_para_julgar(self):
        dois = {"A": (28.4, -81.5), "B": (0.0, 0.0)}
        bons, suspeitos = self.coords.coordenadas_sanas(dois)
        self.assertEqual(len(bons), 2, "com menos de 3 não há mediana confiável")
        self.assertEqual(suspeitos, [])


class TestHttpOverpass(BaseTeste):
    def test_post_json_usa_post_e_identifica_o_user_agent(self):
        self.requests.roteador_post = lambda url, payload: Resposta({"elements": []})
        self.monitor.post_json("http://overpass", {"data": "consulta"})
        self.assertEqual(self.requests.posts, [{"data": "consulta"}])
        self.assertIn("Fila-Disney", self.requests.headers_enviados["User-Agent"],
                      "Overpass devolve 406 para o User-Agent padrão do requests")

    def test_get_json_tambem_manda_user_agent(self):
        self.requests.roteador = lambda url: Resposta({"ok": 1})
        self.monitor.get_json("http://x")
        self.assertIn("Fila-Disney", self.requests.headers_enviados["User-Agent"])

    def test_post_tambem_retenta(self):
        respostas = [Resposta(status=500), Resposta({"elements": []})]
        self.requests.roteador_post = lambda url, payload: respostas.pop(0)
        self.assertEqual(self.monitor.post_json("http://overpass", {"data": "q"}),
                         {"elements": []})
        self.assertEqual(len(self.requests.posts), 2)


class TestUsoModeradoDaOverpass(BaseTeste):
    """A primeira execução real levou 504, 429 e recusa de conexão."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")

    def test_429_sem_retry_after_espera_o_minimo(self):
        esperas = []
        self.monitor._dormir = esperas.append
        respostas = [Resposta(status=429), Resposta({"elements": []})]
        self.requests.roteador_post = lambda url, payload: respostas.pop(0)
        self.monitor.post_json("http://overpass", {"data": "q"}, espera_minima=45)
        self.assertEqual(esperas, [45.0], "backoff curto demais é o que derruba a consulta")

    def test_retry_after_maior_que_o_minimo_e_respeitado(self):
        esperas = []
        self.monitor._dormir = esperas.append
        respostas = [Resposta(status=429, headers={"Retry-After": "90"}),
                     Resposta({"elements": []})]
        self.requests.roteador_post = lambda url, payload: respostas.pop(0)
        self.monitor.post_json("http://overpass", {"data": "q"}, espera_minima=45)
        self.assertEqual(esperas, [90.0])

    def test_falha_de_rede_tambem_respeita_espera_minima(self):
        esperas = []
        self.monitor._dormir = esperas.append
        self.requests.roteador_post = lambda url, payload: Resposta(status=504)
        with self.assertRaises(self.requests.RequestException):
            self.monitor.post_json("http://overpass", {"data": "q"},
                                   tentativas=3, espera_minima=45)
        self.assertEqual(esperas, [45.0, 45.0])

    def test_parque_completo_e_pulado(self):
        saida = {"rides": {"Epcot": {a: [1.0, 2.0] for a in self.config["parks"]["Epcot"]["attractions"]}}}
        self.assertTrue(self.coords.parque_completo("Epcot", self.config, saida))

    def test_parque_incompleto_nao_e_pulado(self):
        saida = {"rides": {"Epcot": {"Test Track": [1.0, 2.0]}}}
        self.assertFalse(self.coords.parque_completo("Epcot", self.config, saida))

    def test_parque_ausente_nao_e_pulado(self):
        self.assertFalse(self.coords.parque_completo("Epcot", self.config, {"rides": {}}))
