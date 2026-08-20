"""Ciclo completo: coleta, grava, alerta e devolve payload."""
import datetime as dt
import unittest

from tests.apoio import BaseTeste, Resposta

EDT = dt.timezone(dt.timedelta(hours=-4))

PAYLOAD = {"lands": [{"name": "Toy Story Land", "rides": [
    {"name": "Slinky Dog Dash", "wait_time": 45, "is_open": True},
    {"name": "Toy Story Mania!", "wait_time": 20, "is_open": True},
    {"name": "Tower of Terror", "wait_time": None, "is_open": False},
    {"name": "Muppet*Vision 3D", "wait_time": 5, "is_open": True},
    {"name": "Star Wars: Rise of the Resistance", "wait_time": 95, "is_open": True},
]}]}


class TestRunCycle(BaseTeste):
    def setUp(self):
        super().setUp()
        self.requests.roteador = lambda url: Resposta(PAYLOAD)
        self.parques = {"Disney Hollywood Studios": 7}
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)

    def linhas_gravadas(self):
        return self.conn.execute("SELECT COUNT(*) FROM wait_times").fetchone()[0]

    def test_grava_todas_as_atracoes_inclusive_fora_da_watchlist(self):
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertEqual(self.linhas_gravadas(), 5, "histórico guarda tudo")

    def test_devolve_payload_para_o_top_alert_reaproveitar(self):
        payloads = self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertEqual(payloads["Disney Hollywood Studios"], PAYLOAD)
        self.assertEqual(len(self.requests.gets), 1, "um GET por parque, sem chamada extra")

    def test_alerta_so_quem_esta_abaixo_do_threshold(self):
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        enviadas = self.enviadas()
        self.assertTrue(any("Toy Story Mania!" in t for t in enviadas), "20 <= 30, alerta")
        self.assertTrue(any("Slinky Dog Dash" in t for t in enviadas), "45 <= 50, alerta")
        self.assertFalse(any("Rise of the Resistance" in t for t in enviadas),
                         "95 > 60, não alerta")
        self.assertFalse(any("Muppet" in t for t in enviadas), "fora da watchlist")

    def test_atracao_fechada_nao_alerta_e_nao_vira_zero(self):
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertFalse(any("Tower of Terror" in t for t in self.enviadas()))
        fechada = self.conn.execute(
            "SELECT wait_time, is_open FROM wait_times WHERE ride = 'Tower of Terror'"
        ).fetchone()
        self.assertEqual(fechada, (None, 0), "sem dado continua NULL, não 0")

    def test_nao_alerta_fora_do_dia_de_parque(self):
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 16, 14, 0, tzinfo=EDT)
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertEqual(self.enviadas(), [], "16/10 é dia de descanso")
        self.assertEqual(self.linhas_gravadas(), 5, "mas continua coletando")

    def test_nao_alerta_em_quiet_hours(self):
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 23, 0, tzinfo=EDT)
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertEqual(self.enviadas(), [])

    def test_cooldown_impede_repetir_no_ciclo_seguinte(self):
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        antes = len(self.enviadas())
        self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertEqual(len(self.enviadas()), antes, "cooldown de 45 min segura")
        self.assertEqual(self.linhas_gravadas(), 10, "mas o histórico continua")

    def test_api_fora_do_ar_nao_derruba_o_ciclo(self):
        self.requests.roteador = lambda url: Resposta(status=500)
        payloads = self.monitor.run_cycle(self.conn, self.config, self.parques)
        self.assertEqual(payloads, {})
        self.assertEqual(self.linhas_gravadas(), 0)

    def test_um_parque_falho_nao_impede_os_outros(self):
        def roteador(url):
            return Resposta(status=500) if "/7/" in url else Resposta(PAYLOAD)
        self.requests.roteador = roteador
        payloads = self.monitor.run_cycle(
            self.conn, self.config, {"Disney Hollywood Studios": 7, "Epcot": 5})
        self.assertNotIn("Disney Hollywood Studios", payloads)
        self.assertIn("Epcot", payloads)


if __name__ == "__main__":
    unittest.main()
