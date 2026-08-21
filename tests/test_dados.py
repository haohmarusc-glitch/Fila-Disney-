"""Config, tendência, resumo diário e fuso — o que mexe com o histórico."""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from tests.apoio import BaseTeste

EDT = dt.timezone(dt.timedelta(hours=-4))


class TestValidacaoDaConfig(BaseTeste):
    def test_config_do_repo_e_valida(self):
        self.assertEqual(self.monitor.validar_config(self.config), [])

    def test_watchlist_nao_inclui_show_nem_atracao_encerrada(self):
        nomes = {
            atracao
            for parque in self.config["parks"].values()
            for atracao in parque.get("attractions", {})
        }
        self.assertNotIn("Hollywood Rip Ride Rockit", nomes)
        self.assertNotIn("Le Cirque Arcanus", nomes)

    def test_park_days_apontando_para_parque_inexistente(self):
        cfg = dict(self.config, park_days={"2026-10-13": ["Parque Fantasma"]})
        problemas = self.monitor.validar_config(cfg)
        self.assertTrue(any("Parque Fantasma" in p for p in problemas))

    def test_data_fora_do_formato_iso(self):
        cfg = dict(self.config, park_days={"13/10/2026": ["Epcot"]})
        self.assertTrue(any("13/10/2026" in p for p in self.monitor.validar_config(cfg)))

    def test_quiet_hours_fora_de_hh_mm(self):
        cfg = dict(self.config, alert={"quiet_hours": {"start": "22h", "end": "07:00"}})
        self.assertTrue(any("quiet_hours" in p for p in self.monitor.validar_config(cfg)))

    def test_sem_parques(self):
        cfg = dict(self.config, parks={})
        self.assertTrue(any("parks" in p for p in self.monitor.validar_config(cfg)))


class TestTendencia(BaseTeste):
    def gravar_serie(self, valores):
        agora = self.monitor.utc_now()
        for i, valor in enumerate(valores):
            ts = agora - dt.timedelta(minutes=5 * (len(valores) - 1 - i))
            self.gravar("Epcot", "Test Track", valor, ts)

    def test_fila_subindo(self):
        self.gravar_serie([14, 22, 31])
        seta, delta = self.monitor.tendencia(self.conn, "Epcot", "Test Track")
        self.assertEqual(seta, "↑")
        self.assertEqual(delta, 17)

    def test_fila_caindo(self):
        self.gravar_serie([60, 45, 31])
        seta, delta = self.monitor.tendencia(self.conn, "Epcot", "Test Track")
        self.assertEqual(seta, "↓")
        self.assertEqual(delta, -29)

    def test_estavel(self):
        self.gravar_serie([30, 32, 31])
        seta, _ = self.monitor.tendencia(self.conn, "Epcot", "Test Track")
        self.assertEqual(seta, "→", "variação abaixo do ruído da API")

    def test_uma_leitura_so_nao_da_tendencia(self):
        self.gravar_serie([30])
        self.assertIsNone(self.monitor.tendencia(self.conn, "Epcot", "Test Track"))

    def test_ignora_leitura_antiga(self):
        antigo = self.monitor.utc_now() - dt.timedelta(hours=6)
        self.gravar("Epcot", "Test Track", 120, antigo)
        self.gravar("Epcot", "Test Track", 30, self.monitor.utc_now())
        self.assertIsNone(self.monitor.tendencia(self.conn, "Epcot", "Test Track"),
                          "leitura de 6h atrás não é tendência de agora")

    def test_marca_vazia_sem_dado(self):
        self.assertEqual(self.monitor.marca_tendencia(self.conn, "Epcot", "Test Track"), "")
        self.assertEqual(self.monitor.marca_tendencia(None, "Epcot", "Test Track"), "")


class TestFuso(BaseTeste):
    def test_offset_muda_entre_edt_e_est(self):
        ny = ZoneInfo("America/New_York")
        for data, esperado in [(dt.datetime(2026, 10, 13, 12), -4),
                               (dt.datetime(2026, 12, 13, 12), -5)]:
            self.monitor.now_park = lambda _c, _d=data: _d.replace(tzinfo=ny)
            self.assertEqual(self.monitor.park_utc_offset_horas(self.config), esperado)

    def test_timestamp_gravado_sem_offset(self):
        """Formato tem que continuar igual ao histórico já no banco."""
        import re
        ts = self.monitor.utc_now().isoformat()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")


class TestResumoDiario(BaseTeste):
    def setUp(self):
        super().setUp()
        base = dt.datetime(2026, 10, 1)
        for dia in range(6):
            for hora_parque in range(9, 21):
                ts = base + dt.timedelta(days=dia, hours=hora_parque + 4)  # EDT
                self.gravar("Disney Hollywood Studios", "Slinky Dog Dash", 30 + hora_parque * 3, ts)
                self.gravar("Disney Hollywood Studios", "Slinky Dog Dash Single Rider", 0, ts)
        self.parques = {"Disney Hollywood Studios": 7}

    def hora(self, h, m=0, dia=13):
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, dia, h, m, tzinfo=EDT)

    def test_manda_na_janela_e_so_uma_vez(self):
        self.hora(6, 30)
        self.monitor.maybe_send_daily_summary(self.conn, self.config, self.parques)
        self.assertEqual(self.enviadas(), [], "antes da hora não manda")

        self.hora(7, 0)
        self.monitor.maybe_send_daily_summary(self.conn, self.config, self.parques)
        self.assertEqual(len(self.enviadas()), 1)

        self.hora(7, 55)
        self.monitor.maybe_send_daily_summary(self.conn, self.config, self.parques)
        self.assertEqual(len(self.enviadas()), 1, "não repete no mesmo dia")

    def test_fora_da_janela_nao_manda(self):
        self.hora(11)
        self.monitor.maybe_send_daily_summary(self.conn, self.config, self.parques)
        self.assertEqual(self.enviadas(), [])

    def test_dia_sem_parque_nao_manda(self):
        self.hora(7, 0, dia=16)
        self.monitor.maybe_send_daily_summary(self.conn, self.config, self.parques)
        self.assertEqual(self.enviadas(), [])

    def test_single_rider_fora_da_previsao(self):
        self.hora(7, 0)
        texto = self.monitor.format_daily_summary(
            self.conn, self.config, "Disney Hollywood Studios")
        self.assertIn("Slinky Dog Dash", texto)
        self.assertNotIn("Single Rider", texto)

    def test_titulo_neutro_fora_de_dia_de_parque(self):
        self.hora(7, 0, dia=16)
        texto = self.monitor.format_daily_summary(
            self.conn, self.config, "Disney Hollywood Studios")
        self.assertNotIn("Hoje é dia de", texto)

    def test_parque_sem_historico_avisa(self):
        self.hora(7, 0)
        texto = self.monitor.format_daily_summary(self.conn, self.config, "Epcot")
        self.assertIn("Ainda não tenho histórico", texto)


if __name__ == "__main__":
    unittest.main()
