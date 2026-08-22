import datetime as dt

from tests.apoio import CHAT_FAKE, BaseTeste, Resposta


PARK = "Disney Hollywood Studios"
POSITION = (28.4180, -81.5810)
COORDS = {
    "parks": {PARK: list(POSITION)},
    "rides": {PARK: {"Mickey & Minnie's Runaway Railway": [28.4189, -81.5810]}},
}
PAYLOAD = {"lands": [{"name": "Animation Courtyard", "rides": [
    {"name": "Meet Olaf at Celebrity Spotlight", "wait_time": 10, "is_open": True},
    {"name": "Meet Ariel at Walt Disney Presents", "wait_time": 5, "is_open": False},
    {"name": "Tower of Terror", "wait_time": 20, "is_open": True},
]}]}


class TestPersonagens(BaseTeste):
    def setUp(self):
        super().setUp()
        self.parks = {PARK: 7}
        self.requests.roteador = lambda _url: Resposta(PAYLOAD)

    def test_filtra_encontro_aberto_e_calcula_distancia(self):
        park, items = self.monitor.buscar_personagens_proximos(
            *POSITION, self.conn, self.parks, COORDS, 500)
        self.assertEqual(park, PARK)
        self.assertEqual([item["name"] for item in items], ["Meet Olaf at Celebrity Spotlight"])
        self.assertLess(items[0]["meters"], 500)

    def test_comandos_ativam_e_desativam_por_chat(self):
        off = self.monitor.handle_command(
            "/alerta_personagens off", self.conn, self.config, self.parks, COORDS, CHAT_FAKE)
        self.assertIn("desativados", off)
        self.assertEqual(self.monitor.preferencia_alerta_personagens(self.conn, CHAT_FAKE)[0], False)
        on = self.monitor.handle_command(
            "/alerta_personagens on", self.conn, self.config, self.parks, COORDS, CHAT_FAKE)
        self.assertIn("ativados", on)

    def test_consulta_manual_usa_ultima_localizacao(self):
        self.monitor.guardar_localizacao(self.conn, *POSITION, CHAT_FAKE)
        text = self.monitor.handle_command(
            "/personagens_perto", self.conn, self.config, self.parks, COORDS, CHAT_FAKE)
        self.assertIn("Meet Olaf", text)
        self.assertIn("Google Maps", text)

    def test_alerta_tem_cooldown_de_uma_hora(self):
        self.assertEqual(self.monitor.enviar_alertas_personagens(
            *POSITION, self.conn, self.parks, COORDS, CHAT_FAKE), 1)
        # Força nova consulta, mas o cooldown do personagem ainda impede repetição.
        self.conn.execute(
            "UPDATE character_last_checks SET checked_at = ? WHERE chat_id = ?",
            ((self.monitor.utc_now() - dt.timedelta(minutes=3)).isoformat(), CHAT_FAKE),
        )
        self.conn.commit()
        self.assertEqual(self.monitor.enviar_alertas_personagens(
            *POSITION, self.conn, self.parks, COORDS, CHAT_FAKE), 0)
        self.assertEqual(len(self.enviadas()), 1)
        self.assertIn("Personagem próximo", self.enviadas()[0])

    def test_localizacao_ao_vivo_editada_nao_repete_ranking(self):
        self.requests.roteador = lambda url: (
            Resposta({"result": [{"update_id": 8, "edited_message": {
                "chat": {"id": int(CHAT_FAKE)},
                "location": {"latitude": POSITION[0], "longitude": POSITION[1], "live_period": 900},
            }}]}) if "getUpdates" in url else Resposta(PAYLOAD)
        )
        self.monitor.serve_commands(None, self.conn, self.config, self.parks, 0, COORDS)
        self.assertFalse(any("Ordenado por" in text for text in self.enviadas()))
        self.assertTrue(any("Personagem próximo" in text for text in self.enviadas()))
