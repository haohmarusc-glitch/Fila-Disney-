"""Regras temporais e de alerta: quiet hours, cooldown, virada do dia, cadência."""
import datetime as dt
import unittest

from tests.apoio import BaseTeste

EDT = dt.timezone(dt.timedelta(hours=-4))


def fixar_hora(monitor, hora, minuto=0, dia=13, mes=10):
    monitor.now_park = lambda _c: dt.datetime(2026, mes, dia, hora, minuto, tzinfo=EDT)


class TestQuietHours(BaseTeste):
    def test_janela_cruzando_meia_noite(self):
        casos = {6: True, 7: False, 12: False, 21: False, 22: True, 23: True, 0: True, 3: True}
        for hora, esperado in casos.items():
            fixar_hora(self.monitor, hora)
            self.assertEqual(self.monitor.in_quiet_hours(self.config), esperado, f"{hora}h")

    def test_sem_quiet_hours_configurado_nunca_silencia(self):
        cfg = dict(self.config, alert={"cooldown_minutes": 45})
        fixar_hora(self.monitor, 3)
        self.assertFalse(self.monitor.in_quiet_hours(cfg))


class TestCooldown(BaseTeste):
    def test_bloqueia_dentro_da_janela_e_libera_depois(self):
        self.monitor.mark_alerted(self.conn, "Epcot", "Test Track")
        self.assertTrue(self.monitor.recently_alerted(self.conn, "Epcot", "Test Track", 45))
        self.assertFalse(self.monitor.recently_alerted(self.conn, "Epcot", "Test Track", 0))

    def test_cooldown_e_por_atracao(self):
        self.monitor.mark_alerted(self.conn, "Epcot", "Test Track")
        self.assertFalse(
            self.monitor.recently_alerted(self.conn, "Epcot", "Frozen Ever After", 45))

    def test_cooldown_e_por_parque(self):
        self.monitor.mark_alerted(self.conn, "Epcot", "Test Track")
        self.assertFalse(
            self.monitor.recently_alerted(self.conn, "Disney Magic Kingdom", "Test Track", 45))


class TestDiaDeParque(BaseTeste):
    def test_virada_do_dia_troca_o_parque(self):
        fixar_hora(self.monitor, 23, 59, dia=14)
        self.assertEqual(self.monitor.is_alert_day(self.config), ["Disney Animal Kingdom"])
        fixar_hora(self.monitor, 0, 1, dia=15)
        self.assertEqual(self.monitor.is_alert_day(self.config), ["Epcot"])

    def test_dia_sem_parque_e_lista_vazia(self):
        fixar_hora(self.monitor, 12, dia=16)
        self.assertEqual(self.monitor.is_alert_day(self.config), [])


class TestTopAlert(BaseTeste):
    def setUp(self):
        super().setUp()
        self.payload = {"lands": [{"name": "L", "rides": [
            {"name": "Slinky Dog Dash", "wait_time": 40, "is_open": True},
            {"name": "Toy Story Mania!", "wait_time": 20, "is_open": True},
            {"name": "Tower of Terror", "wait_time": 30, "is_open": True},
            {"name": "Star Wars: Rise of the Resistance", "wait_time": 95, "is_open": True},
            {"name": "Slinky Dog Dash Single Rider", "wait_time": 0, "is_open": True},
            {"name": "Alien Swirling Saucers", "wait_time": 10, "is_open": False},
        ]}]}
        self.parques = {"Disney Hollywood Studios": 7}
        fixar_hora(self.monitor, 14)

    def enviar(self):
        self.monitor.maybe_send_top_alert(
            self.conn, self.config, self.parques, {"Disney Hollywood Studios": self.payload})

    def test_manda_as_tres_menores_da_watchlist(self):
        self.enviar()
        texto = self.enviadas()[-1]
        self.assertIn("Toy Story Mania!", texto)
        self.assertIn("Tower of Terror", texto)
        self.assertIn("Slinky Dog Dash", texto)
        self.assertNotIn("Rise of the Resistance", texto, "4ª colocada fica fora")

    def test_ignora_single_rider_e_fechada(self):
        self.enviar()
        texto = self.enviadas()[-1]
        self.assertNotIn("Single Rider", texto)
        self.assertNotIn("Alien Swirling", texto, "atração fechada não entra")

    def test_nao_repete_dentro_do_intervalo(self):
        self.enviar()
        antes = len(self.enviadas())
        self.enviar()
        self.assertEqual(len(self.enviadas()), antes)

    def test_libera_depois_do_intervalo(self):
        self.enviar()
        antes = len(self.enviadas())
        vencido = (self.monitor.utc_now() - dt.timedelta(minutes=11)).isoformat()
        self.conn.execute("UPDATE top_alert SET sent_at = ?", (vencido,))
        self.conn.commit()
        self.enviar()
        self.assertEqual(len(self.enviadas()), antes + 1)

    def test_reinicio_do_container_nao_dispara_de_novo(self):
        """A cadência mora no banco: reimportar o módulo não pode ressetá-la."""
        self.enviar()
        antes = len(self.enviadas())
        import importlib
        self.monitor = importlib.reload(self.monitor)
        self.monitor.DB_PATH = self.conn.execute("PRAGMA database_list").fetchone()[2]
        fixar_hora(self.monitor, 14)
        self.enviar()
        self.assertEqual(len(self.enviadas()), antes)

    def test_calado_em_quiet_hours(self):
        fixar_hora(self.monitor, 23)
        antes = len(self.enviadas())
        self.enviar()
        self.assertEqual(len(self.enviadas()), antes)

    def test_calado_em_dia_sem_parque(self):
        fixar_hora(self.monitor, 14, dia=16)
        antes = len(self.enviadas())
        self.enviar()
        self.assertEqual(len(self.enviadas()), antes)

    def test_fetch_falho_pula_a_rodada(self):
        antes = len(self.enviadas())
        self.monitor.maybe_send_top_alert(self.conn, self.config, self.parques, {})
        self.assertEqual(len(self.enviadas()), antes)


if __name__ == "__main__":
    unittest.main()
