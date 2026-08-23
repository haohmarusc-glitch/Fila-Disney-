"""Freio do acesso familiar.

O bot é alcançável por qualquer pessoa que descubra o nome dele, e o repositório
é público: a senha do /entrar é a única barreira, então ela precisa de limite de
tentativas, de registro e de como ser retirada depois.
"""
import unittest

from tests.apoio import BaseTeste, CHAT_FAKE, Resposta

ESTRANHO = 999999
SENHA = "senha-longa-de-teste"


class TestFreioEntrar(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.FAMILY_ACCESS_PASSWORD = SENHA

    def test_erro_informa_quantas_tentativas_restam(self):
        resposta = self.monitor.autenticar_familiar(self.conn, ESTRANHO, "errada")
        self.assertIn("incorreta", resposta)
        self.assertIn("4", resposta, "deve avisar quantas tentativas sobraram")
        self.assertFalse(self.monitor.chat_autorizado(self.conn, ESTRANHO))

    def test_bloqueia_apos_cinco_erros_e_nao_testa_mais_a_senha(self):
        for _ in range(self.monitor.ENTRAR_TENTATIVAS_MAX):
            self.monitor.autenticar_familiar(self.conn, ESTRANHO, "errada")
        self.assertTrue(self.monitor.entrar_bloqueado(self.conn, ESTRANHO))

        # a senha CERTA não passa enquanto o bloqueio vale: é isso que impede o
        # chute em massa de acertar por insistência.
        resposta = self.monitor.autenticar_familiar(self.conn, ESTRANHO, SENHA)
        self.assertIn("Muitas tentativas", resposta)
        self.assertFalse(self.monitor.chat_autorizado(self.conn, ESTRANHO))

    def test_bloqueio_e_por_chat_e_nao_atinge_os_outros(self):
        for _ in range(self.monitor.ENTRAR_TENTATIVAS_MAX):
            self.monitor.autenticar_familiar(self.conn, ESTRANHO, "errada")
        self.assertFalse(self.monitor.entrar_bloqueado(self.conn, 555))
        self.assertIn("liberado", self.monitor.autenticar_familiar(self.conn, 555, SENHA))

    def test_tentativa_velha_sai_da_janela(self):
        from datetime import timedelta
        antigo = (self.monitor.utc_now()
                  - timedelta(minutes=self.monitor.ENTRAR_JANELA_MINUTOS + 1)).isoformat()
        for _ in range(self.monitor.ENTRAR_TENTATIVAS_MAX):
            self.conn.execute(
                "INSERT INTO auth_attempts (chat_id, attempted_at) VALUES (?, ?)",
                (str(ESTRANHO), antigo),
            )
        self.conn.commit()
        self.assertFalse(self.monitor.entrar_bloqueado(self.conn, ESTRANHO))

    def test_acerto_zera_o_historico_de_erros(self):
        self.monitor.autenticar_familiar(self.conn, ESTRANHO, "errada")
        self.monitor.autenticar_familiar(self.conn, ESTRANHO, SENHA)
        self.assertEqual(self.monitor.tentativas_entrar_recentes(self.conn, ESTRANHO), 0)
        self.assertTrue(self.monitor.chat_autorizado(self.conn, ESTRANHO))

    def test_sem_senha_configurada_nao_conta_tentativa(self):
        self.monitor.FAMILY_ACCESS_PASSWORD = ""
        resposta = self.monitor.autenticar_familiar(self.conn, ESTRANHO, "qualquer")
        self.assertIn("não foi configurado", resposta)
        self.assertEqual(self.monitor.tentativas_entrar_recentes(self.conn, ESTRANHO), 0)


class TestAvisoUnico(BaseTeste):
    def _update(self, chat_id, texto, update_id):
        return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": texto}}

    def test_estranho_e_avisado_uma_vez_so(self):
        self.requests.roteador = lambda url: Resposta({"result": [
            self._update(ESTRANHO, "/status", 1),
            self._update(ESTRANHO, "/status", 2),
            self._update(ESTRANHO, "/parques", 3),
        ]})
        self.monitor.serve_commands(None, self.conn, self.config, {}, 0)
        avisos = [t for t in self.enviadas() if "Acesso restrito" in t]
        self.assertEqual(len(avisos), 1, "só o primeiro contato recebe resposta")

    def test_aviso_volta_depois_da_janela(self):
        self.assertTrue(self.monitor.deve_avisar_nao_autorizado(self.conn, ESTRANHO))
        self.assertFalse(self.monitor.deve_avisar_nao_autorizado(self.conn, ESTRANHO))
        from datetime import timedelta
        antigo = (self.monitor.utc_now()
                  - timedelta(hours=self.monitor.AVISO_RESTRITO_HORAS + 1)).isoformat()
        self.conn.execute("UPDATE unauthorized_notices SET notified_at = ? WHERE chat_id = ?",
                          (antigo, str(ESTRANHO)))
        self.conn.commit()
        self.assertTrue(self.monitor.deve_avisar_nao_autorizado(self.conn, ESTRANHO))

    def test_aviso_e_por_chat(self):
        self.assertTrue(self.monitor.deve_avisar_nao_autorizado(self.conn, ESTRANHO))
        self.assertTrue(self.monitor.deve_avisar_nao_autorizado(self.conn, 4242424))


class TestRevogacao(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.FAMILY_ACCESS_PASSWORD = SENHA
        self.monitor.autenticar_familiar(self.conn, ESTRANHO, SENHA)

    def test_sair_remove_o_proprio_chat(self):
        resposta = self.monitor.handle_command(
            "/sair", self.conn, self.config, {}, None, ESTRANHO)
        self.assertIn("revogado", resposta)
        self.assertFalse(self.monitor.chat_autorizado(self.conn, ESTRANHO))

    def test_chat_principal_nao_pode_se_revogar(self):
        resposta = self.monitor.handle_command(
            "/sair", self.conn, self.config, {}, None, int(CHAT_FAKE))
        self.assertIn("não pode ser revogado", resposta)
        self.assertTrue(self.monitor.chat_autorizado(self.conn, int(CHAT_FAKE)))

    def test_revogar_de_terceiro_so_no_chat_principal(self):
        negado = self.monitor.handle_command(
            f"/revogar {ESTRANHO}", self.conn, self.config, {}, None, 777)
        self.assertIn("Só o chat principal", negado)
        self.assertTrue(self.monitor.chat_autorizado(self.conn, ESTRANHO))

        ok = self.monitor.handle_command(
            f"/revogar {ESTRANHO}", self.conn, self.config, {}, None, int(CHAT_FAKE))
        self.assertIn("revogado", ok)
        self.assertFalse(self.monitor.chat_autorizado(self.conn, ESTRANHO))

    def test_revogar_sem_argumento_lista_os_liberados(self):
        resposta = self.monitor.handle_command(
            "/revogar", self.conn, self.config, {}, None, int(CHAT_FAKE))
        self.assertIn(str(ESTRANHO), resposta)

    def test_revogar_chat_que_nao_estava_liberado(self):
        resposta = self.monitor.handle_command(
            "/revogar 12345", self.conn, self.config, {}, None, int(CHAT_FAKE))
        self.assertIn("não estava liberado", resposta)


class TestBancoCompartilhado(BaseTeste):
    def test_monitor_abre_em_wal_com_espera_por_lock(self):
        modo = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(modo.lower(), "wal", "sem WAL a API e o monitor se bloqueiam")
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_conexao_de_leitura_recusa_escrita(self):
        import sqlite3
        leitura = self.monitor.conectar_somente_leitura()
        self.addCleanup(leitura.close)
        self.assertIsNotNone(leitura.execute("SELECT COUNT(*) FROM wait_times").fetchone())
        with self.assertRaises(sqlite3.OperationalError):
            leitura.execute(
                "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
                "VALUES ('2026-10-13T12:00:00', 'p', 'l', 'r', 10, 1)"
            )


if __name__ == "__main__":
    unittest.main()
