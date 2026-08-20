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
