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

    def test_trip_end_ausente_e_apontado(self):
        """É `trip.end` que destrava a poda; sem ele o histórico nunca expira."""
        cfg = dict(self.config, trip={"timezone": "America/New_York"})
        self.assertTrue(any("trip.end" in p for p in self.monitor.validar_config(cfg)))

    def test_trip_end_fora_do_formato_iso(self):
        cfg = dict(self.config, trip=dict(self.config["trip"], end="25/10/2026"))
        self.assertTrue(any("25/10/2026" in p for p in self.monitor.validar_config(cfg)))


class TestPodaDoHistorico(BaseTeste):
    """A poda de `wait_times` é o único lugar que pode apagar histórico."""

    def semear_historico(self):
        """Grava direto, sem passar pelo fetch: o que se testa aqui é a poda.

        Uma versão anterior destes testes chamava `run_cycle`, o fetch falhava
        no harness e a tabela ficava vazia — `0 == 0` passava com qualquer poda.
        """
        agora = self.monitor.utc_now()
        for dias in (400, 200, 1):
            self.gravar("Epcot", "Test Track", 30, agora - dt.timedelta(days=dias))
        return self.conn.execute("SELECT COUNT(*) FROM wait_times").fetchone()[0]

    def test_nao_poda_antes_de_30_dias_apos_a_viagem(self):
        """Há linha de 400 dias atrás, mais velha que qualquer retenção."""
        antes = self.semear_historico()
        self.assertEqual(antes, 3)
        self.monitor.maybe_maintain_db(self.conn, self.config)
        depois = self.conn.execute("SELECT COUNT(*) FROM wait_times").fetchone()[0]
        self.assertEqual(depois, 3, "histórico não pode sumir antes da viagem")

    def test_sem_trip_end_a_manutencao_roda_e_nao_poda(self):
        """`date.max` fazia `date.max + 30 dias` estourar OverflowError.

        O efeito não era apagar demais — era a manutenção inteira morrer todo
        dia, deixando as tabelas de log crescerem sem poda, em silêncio.
        """
        cfg = dict(self.config, trip={"timezone": "America/New_York"})
        antes = self.semear_historico()
        self.monitor.maybe_maintain_db(self.conn, cfg)   # não pode levantar
        depois = self.conn.execute("SELECT COUNT(*) FROM wait_times").fetchone()[0]
        self.assertEqual(depois, antes)

    def test_poda_de_verdade_depois_da_janela(self):
        """Com a viagem no passado, o que passou da retenção sai — e só isso."""
        agora = self.monitor.utc_now()
        cfg = dict(self.config,
                   trip=dict(self.config["trip"],
                             end=(agora.date() - dt.timedelta(days=60)).isoformat()),
                   database={"raw_retention_days": 300})
        self.semear_historico()
        self.monitor.maybe_maintain_db(self.conn, cfg)
        restantes = [r[0] for r in
                     self.conn.execute("SELECT ts FROM wait_times ORDER BY ts")]
        # Retenção de 300 dias põe a fronteira entre as linhas de 400 e 200:
        # sai só a mais velha. Com 180 as duas sairiam, e o teste não distinguiria
        # "poda pelo corte" de "poda tudo".
        self.assertEqual(len(restantes), 2, "só a linha de 400 dias deveria sair")
        mais_velha = agora - dt.timedelta(days=200)
        self.assertEqual(restantes[0][:10], mais_velha.date().isoformat())


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
    def test_hora_local_muda_entre_edt_e_est(self):
        """A conversão é por balde, então outubro e dezembro não se misturam."""
        ny = ZoneInfo("America/New_York")
        self.assertEqual(self.monitor.hora_no_parque("2026-10-13", 18, ny), 14)  # EDT
        self.assertEqual(self.monitor.hora_no_parque("2026-12-13", 18, ny), 13)  # EST

    def test_fuso_vem_do_watchlist(self):
        self.assertEqual(str(self.monitor.fuso_do_parque(self.config)), "America/New_York")

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
