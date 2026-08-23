"""Ranking de quebras e o custo das consultas de histórico.

O histórico cresce ~50 mil linhas por dia. Duas coisas dependem disso não
degradar: o resumo automático das 7h, que roda em sete dias de parque, e o
/quebras, que varre o mesmo período. Daí o índice por `ts` e a janela.
"""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from tests.apoio import BaseTeste

NY = ZoneInfo("America/New_York")
PARQUE = "Epcot"
ATRACOES = ["Frozen Ever After", "Test Track", "Soarin'"]


class BaseHistorico(BaseTeste):
    def gravar_ciclos(self, quantos, hora_utc=18, abertas=None, inicio_dias=1):
        """Insere `quantos` ciclos, um a cada 5 min, todos no mesmo horário.

        `abertas` decide quais atrações estão abertas em cada ciclo (por índice).
        """
        base = self.monitor.utc_now() - dt.timedelta(days=inicio_dias)
        base = base.replace(hour=hora_utc, minute=0, second=0, microsecond=0)
        linhas = []
        for i in range(quantos):
            ts = (base + dt.timedelta(minutes=5 * i)).isoformat()
            for atracao in ATRACOES:
                aberta = abertas(i, atracao) if abertas else True
                linhas.append((ts, PARQUE, "Land", atracao, 30 if aberta else None,
                               int(aberta)))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()


class TestIndices(BaseTeste):
    def test_existe_indice_por_ts(self):
        indices = {linha[0] for linha in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'wait_times'"
        ).fetchall()}
        self.assertIn("idx_wait_ts", indices,
                      "sem ele o /ranking hoje varre a tabela inteira")

    def test_consulta_por_ts_usa_o_indice(self):
        plano = " ".join(str(c) for c in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM wait_times "
            "WHERE ts >= '2026-01-01' AND ts <= '2026-12-31'").fetchall())
        self.assertIn("idx_wait_ts", plano)
        self.assertNotIn("SCAN wait_times\n", plano)


class TestJanelaDaPrevisao(BaseHistorico):
    def test_dado_antigo_fica_fora_da_previsao(self):
        antigo = (self.monitor.utc_now() - dt.timedelta(days=400)).isoformat()
        self.conn.execute(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, 'Land', 'Frozen Ever After', 999, 1)", (antigo, PARQUE))
        self.conn.commit()
        self.gravar_ciclos(12)
        previsao = self.monitor.previsao_por_atracao(self.conn, self.config, PARQUE)
        picos = {ride: pico[1] for ride, _ab, _me, pico, _n in previsao}
        self.assertIn("Frozen Ever After", picos)
        self.assertLess(picos["Frozen Ever After"], 100,
                        "leitura de 400 dias atrás não pode entrar na média")

    def test_janela_e_configuravel_com_piso(self):
        config = dict(self.config, daily_summary={"lookback_days": 1})
        self.gravar_ciclos(12, inicio_dias=5)  # dentro do piso de 7 dias
        previsao = self.monitor.previsao_por_atracao(self.conn, config, PARQUE)
        self.assertTrue(previsao, "o piso de 7 dias protege contra janela curta demais")


class TestQuebras(BaseHistorico):
    def quebra_test_track(self, i, atracao):
        # Test Track fechada em 20% dos ciclos; as outras sempre abertas.
        return not (atracao == "Test Track" and i % 5 == 0)

    def test_amostra_pequena_nao_vira_ranking(self):
        self.gravar_ciclos(10, abertas=self.quebra_test_track)
        ranking, ciclos = self.monitor.historico_quebras(self.conn, self.config, PARQUE)
        self.assertEqual(ranking, [])
        self.assertEqual(ciclos, 10)
        self.assertIn("histórico suficiente",
                      self.monitor.format_quebras(self.conn, self.config, PARQUE))

    def test_ranking_mede_a_fracao_fechada(self):
        self.gravar_ciclos(60, abertas=self.quebra_test_track)
        ranking, ciclos = self.monitor.historico_quebras(self.conn, self.config, PARQUE)
        self.assertEqual(ciclos, 60)
        self.assertEqual(len(ranking), 1, "só a que fechou entra no ranking")
        ride, pct, fechadas, total, _pior = ranking[0]
        self.assertEqual(ride, "Test Track")
        self.assertEqual(fechadas, 12)
        self.assertEqual(total, 60)
        self.assertAlmostEqual(pct, 20.0)

    def test_parque_fechado_nao_conta_como_quebra(self):
        # Madrugada: tudo fechado. Sem o filtro, viraria "100% quebrada".
        self.gravar_ciclos(60, hora_utc=6, abertas=lambda i, a: False)
        ranking, ciclos = self.monitor.historico_quebras(self.conn, self.config, PARQUE)
        self.assertEqual(ciclos, 0, "ciclo sem ninguém aberto não é operação")
        self.assertEqual(ranking, [])

    def test_atracao_sempre_aberta_fica_fora(self):
        self.gravar_ciclos(60, abertas=self.quebra_test_track)
        ranking, _ciclos = self.monitor.historico_quebras(self.conn, self.config, PARQUE)
        self.assertNotIn("Soarin'", [item[0] for item in ranking])

    def test_pior_hora_sai_no_fuso_do_parque(self):
        self.gravar_ciclos(60, hora_utc=18, abertas=self.quebra_test_track)
        ranking, _ciclos = self.monitor.historico_quebras(self.conn, self.config, PARQUE)
        self.assertEqual(ranking[0][4], 14, "18h UTC é 14h no parque, em horário de verão")

    def test_single_rider_fica_fora_do_ranking(self):
        self.gravar_ciclos(60, abertas=self.quebra_test_track)
        base = self.monitor.utc_now() - dt.timedelta(days=1)
        base = base.replace(hour=18, minute=0, second=0, microsecond=0)
        for i in range(60):
            self.gravar(PARQUE, "Test Track Single Rider", None,
                        base + dt.timedelta(minutes=5 * i), aberta=False)
        ranking, _ciclos = self.monitor.historico_quebras(self.conn, self.config, PARQUE)
        self.assertNotIn("Test Track Single Rider", [item[0] for item in ranking])

    def test_mensagem_traz_percentual_base_e_ressalva(self):
        self.gravar_ciclos(60, abertas=self.quebra_test_track)
        texto = self.monitor.format_quebras(self.conn, self.config, PARQUE)
        self.assertIn("Test Track", texto)
        self.assertIn("20%", texto)
        self.assertIn("60 ciclos", texto)
        self.assertIn("não causa", texto, "não pode passar por diagnóstico de quebra")
        self.assertIn("/fechadas", texto, "tem que apontar para o comando do agora")

    def test_parque_estavel_diz_isso_em_vez_de_lista_vazia(self):
        self.gravar_ciclos(60)
        self.assertIn("estável", self.monitor.format_quebras(self.conn, self.config, PARQUE))

    def test_comando_responde_sem_chamar_a_api(self):
        self.gravar_ciclos(60, abertas=self.quebra_test_track)
        antes = len(self.requests.gets)
        texto = self.monitor.handle_command(
            f"/quebras {PARQUE}", self.conn, self.config, {PARQUE: 5})
        self.assertIn("Test Track", texto)
        self.assertEqual(len(self.requests.gets), antes,
                         "/quebras é histórico puro; não gasta chamada na API")


if __name__ == "__main__":
    unittest.main()
