"""Lembretes de prazo e o fuso da análise.

O monitor sabe a fila, mas quem perde a janela das 7h do Multi-Pass paga em fila
o dia inteiro — e o `docs/ROTEIRO.md` listava esses prazos como "o que o monitor
NÃO cobre". Aqui eles passam a ser cobertos.
"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import analyze
from tests.apoio import BaseTeste

TZ = ZoneInfo("America/New_York")


class TestLembretes(BaseTeste):
    def setUp(self):
        super().setUp()
        self.config["reminders"] = [{
            "id": "multipass-hs",
            "date": "2026-10-10",
            "hour": "07:00",
            "text": "Abre a compra do Multi-Pass do Hollywood Studios.",
        }]

    def em(self, momento):
        self.monitor.now_park = lambda _cfg: momento

    def test_sai_na_hora_marcada(self):
        self.em(datetime(2026, 10, 10, 7, 0, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertIn("Multi-Pass", self.enviadas()[0])
        self.assertIn("Lembrete", self.enviadas()[0])

    def test_nao_sai_antes_da_hora(self):
        self.em(datetime(2026, 10, 10, 6, 59, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(self.enviadas(), [])

    def test_nao_sai_no_dia_errado(self):
        self.em(datetime(2026, 10, 9, 7, 0, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(self.enviadas(), [])

    def test_container_que_subiu_tarde_ainda_recebe(self):
        self.em(datetime(2026, 10, 10, 8, 30, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(len(self.enviadas()), 1)

    def test_fora_da_janela_nao_recebe(self):
        self.em(datetime(2026, 10, 10, 9, 30, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(self.enviadas(), [])

    def test_nunca_repete_no_mesmo_dia(self):
        self.em(datetime(2026, 10, 10, 7, 0, tzinfo=TZ))
        for _ in range(5):  # cinco ciclos dentro da janela
            self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(len(self.enviadas()), 1)

    def test_falha_no_envio_nao_marca_como_enviado(self):
        from tests.apoio import Resposta
        self.requests.roteador_post = lambda url, payload: Resposta({}, status=500)
        self.em(datetime(2026, 10, 10, 7, 0, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertFalse(self.monitor.lembrete_enviado(self.conn, "multipass-hs"))

    def test_id_e_a_chave_e_nao_a_posicao_na_lista(self):
        self.em(datetime(2026, 10, 10, 7, 0, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        # lembrete novo entra ANTES do já enviado; o antigo não pode sair de novo
        self.config["reminders"].insert(0, {
            "id": "outro", "date": "2026-10-10", "hour": "07:00", "text": "Outro"})
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(len(self.enviadas()), 2)
        self.assertIn("Outro", self.enviadas()[1])

    def test_lembrete_sem_id_nao_e_enviado(self):
        self.config["reminders"] = [
            {"date": "2026-10-10", "hour": "07:00", "text": "Sem id"}]
        self.em(datetime(2026, 10, 10, 7, 0, tzinfo=TZ))
        self.monitor.maybe_send_reminders(self.conn, self.config)
        self.assertEqual(self.enviadas(), [], "sem id ele repetiria a cada ciclo")


class TestValidacaoDosLembretes(BaseTeste):
    def problemas(self, reminders):
        config = dict(self.config, reminders=reminders)
        return " | ".join(self.monitor.validar_config(config))

    def test_config_de_producao_e_valida(self):
        self.assertEqual(self.monitor.validar_config(self.config), [])

    def test_acusa_id_ausente_e_repetido(self):
        self.assertIn("sem 'id'", self.problemas(
            [{"date": "2026-10-10", "text": "x"}]))
        self.assertIn("repetido", self.problemas([
            {"id": "a", "date": "2026-10-10", "text": "x"},
            {"id": "a", "date": "2026-10-11", "text": "y"},
        ]))

    def test_acusa_data_e_hora_invalidas(self):
        self.assertIn("não é uma data ISO", self.problemas(
            [{"id": "a", "date": "10/10/2026", "text": "x"}]))
        self.assertIn("não está em HH:MM", self.problemas(
            [{"id": "a", "date": "2026-10-10", "hour": "7h", "text": "x"}]))

    def test_acusa_texto_vazio(self):
        self.assertIn("sem 'text'", self.problemas(
            [{"id": "a", "date": "2026-10-10", "text": ""}]))


class TestComandoLembretes(BaseTeste):
    def test_lista_os_pendentes_e_esconde_os_passados(self):
        self.monitor.now_park = lambda _cfg: datetime(2026, 10, 11, 12, 0, tzinfo=TZ)
        self.config["reminders"] = [
            {"id": "passado", "date": "2026-10-10", "text": "Ja foi"},
            {"id": "futuro", "date": "2026-10-14", "text": "Multi-Pass do MK"},
        ]
        texto = self.monitor.handle_command("/lembretes", self.conn, self.config, {})
        self.assertIn("Multi-Pass do MK", texto)
        self.assertNotIn("Ja foi", texto)
        self.assertIn("em 3 dias", texto)

    def test_sem_pendente(self):
        self.monitor.now_park = lambda _cfg: datetime(2027, 1, 1, 12, 0, tzinfo=TZ)
        self.assertIn("Nenhum lembrete pendente",
                      self.monitor.handle_command("/lembretes", self.conn, self.config, {}))

    def test_lembretes_de_producao_aparecem(self):
        self.monitor.now_park = lambda _cfg: datetime(2026, 9, 1, 12, 0, tzinfo=TZ)
        texto = self.monitor.handle_command("/lembretes", self.conn, self.config, {})
        self.assertIn("Multi-Pass", texto)


class TestFusoDaAnalise(unittest.TestCase):
    """O `-4` cravado erraria em 1h todo o histórico assim que virasse novembro."""

    def test_horario_de_verao_e_padrao_dao_offsets_diferentes(self):
        self.assertEqual(analyze.hora_local("2026-10-15", 18), 14)  # EDT, -4
        self.assertEqual(analyze.hora_local("2026-11-15", 18), 13)  # EST, -5

    def test_virada_de_dia_em_utc(self):
        # 02:00 UTC do dia 16 é ainda 22h do dia 15 no parque.
        self.assertEqual(analyze.hora_local("2026-10-16", 2), 22)

    def test_media_e_ponderada_pela_quantidade_de_leituras(self):
        linhas = [("2026-10-15", 18, 10.0, 1), ("2026-10-15", 18, 20.0, 3)]
        media, n = analyze.agregar_por_hora_local(linhas)[14]
        self.assertEqual(n, 4)
        self.assertAlmostEqual(media, 17.5)  # e não 15, que seria a média simples

    def test_baldes_de_dias_diferentes_caem_na_mesma_hora_local(self):
        linhas = [("2026-10-15", 18, 30.0, 2), ("2026-10-16", 18, 10.0, 2)]
        resultado = analyze.agregar_por_hora_local(linhas)
        self.assertEqual(list(resultado), [14])
        self.assertAlmostEqual(resultado[14][0], 20.0)

    def test_outubro_e_novembro_nao_se_misturam_na_mesma_hora(self):
        linhas = [("2026-10-15", 18, 30.0, 1), ("2026-11-15", 18, 30.0, 1)]
        self.assertEqual(sorted(analyze.agregar_por_hora_local(linhas)), [13, 14])

    def test_balde_vazio_e_ignorado(self):
        self.assertEqual(analyze.agregar_por_hora_local(
            [("2026-10-15", 18, None, 0), ("2026-10-15", 18, 10.0, 0)]), {})


if __name__ == "__main__":
    unittest.main()
