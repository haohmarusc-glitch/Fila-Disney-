"""Manutenção diária: o que expira, o que não expira e quando o VACUUM roda.

Nada disto tinha teste. A rotina apagava logs de operação mas guardava a
posição GPS da família para sempre, e o arquivo do banco só crescia — DELETE no
SQLite devolve a página para a lista livre, não para o disco.
"""
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.apoio import BaseTeste


class BancoDe:
    """Banco com o tamanho que o teste quiser, sem gerar gigabytes de verdade."""

    def __init__(self, bytes_):
        self._bytes = bytes_

    def stat(self):
        return type("St", (), {"st_size": self._bytes})


class BaseManutencao(BaseTeste):
    def dias_atras(self, dias):
        return (self.monitor.utc_now() - timedelta(days=dias)).isoformat()

    def contar(self, tabela):
        return self.conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


class TestRetencaoDoGPS(BaseManutencao):
    CHAT_VELHO, CHAT_NOVO = "111", "222"

    def gravar_posicoes(self):
        velho, novo = self.dias_atras(30), self.dias_atras(1)
        self.conn.executemany(
            "INSERT INTO user_locations (chat_id, latitude, longitude, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [(self.CHAT_VELHO, 28.4, -81.5, velho), (self.CHAT_NOVO, 28.4, -81.5, novo)])
        self.conn.executemany(
            "INSERT INTO character_last_checks (chat_id, latitude, longitude, checked_at) "
            "VALUES (?, ?, ?, ?)",
            [(self.CHAT_VELHO, 28.4, -81.5, velho), (self.CHAT_NOVO, 28.4, -81.5, novo)])
        self.conn.executemany(
            "INSERT INTO character_alerts (chat_id, park, character_name, sent_at) "
            "VALUES (?, ?, ?, ?)",
            [(self.CHAT_VELHO, "Epcot", "Mickey", velho),
             (self.CHAT_NOVO, "Epcot", "Mickey", novo)])
        self.conn.commit()

    def test_posicao_antiga_da_familia_e_apagada(self):
        self.gravar_posicoes()
        self.monitor.maybe_maintain_db(self.conn, self.config)
        for tabela in ("user_locations", "character_last_checks", "character_alerts"):
            with self.subTest(tabela=tabela):
                self.assertEqual(self.contar(tabela), 1,
                                 "a linha de 30 dias tinha que sair")

    def test_posicao_recente_sobrevive(self):
        """A janela de retenção é folgada de propósito: 7 dias contra 180 min de leitura."""
        self.gravar_posicoes()
        self.monitor.maybe_maintain_db(self.conn, self.config)
        restante = self.conn.execute(
            "SELECT chat_id FROM user_locations").fetchall()
        self.assertEqual([r[0] for r in restante], [self.CHAT_NOVO])

    def test_manutencao_nao_cega_o_perto(self):
        """Apagar não pode quebrar quem acabou de mandar a localização."""
        self.monitor.guardar_localizacao(self.conn, 28.4, -81.5, chat_id=self.CHAT_NOVO)
        self.monitor.maybe_maintain_db(self.conn, self.config)
        self.assertEqual(
            self.monitor.ultima_localizacao(self.conn, chat_id=self.CHAT_NOVO),
            (28.4, -81.5))

    def test_roda_uma_vez_por_dia(self):
        self.gravar_posicoes()
        self.monitor.maybe_maintain_db(self.conn, self.config)
        self.conn.execute(
            "INSERT INTO user_locations (chat_id, latitude, longitude, updated_at) "
            "VALUES (?, ?, ?, ?)", ("333", 28.4, -81.5, self.dias_atras(30)))
        self.conn.commit()
        self.monitor.maybe_maintain_db(self.conn, self.config)
        self.assertEqual(self.contar("user_locations"), 2,
                         "a segunda chamada do mesmo dia não pode apagar de novo")

    def test_historico_bruto_nao_expira_antes_da_viagem(self):
        self.gravar("Epcot", "Test Track", 30,
                    self.monitor.utc_now() - timedelta(days=400))
        self.monitor.maybe_maintain_db(self.conn, self.config)
        self.assertEqual(self.contar("wait_times"), 1,
                         "histórico de antes da viagem é o que treina a previsão")


class TestCompactacao(BaseManutencao):
    def encher_e_esvaziar(self, linhas=120_000):
        """Deixa espaço morto de verdade no arquivo, sem mock."""
        velho = self.dias_atras(200)
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(velho, "Epcot", "World Celebration", f"Atracao numero {i % 50}", 30, 1)
             for i in range(linhas)])
        self.conn.commit()
        self.conn.execute("DELETE FROM wait_times")
        self.conn.commit()

    def test_nao_compacta_banco_sem_espaco_morto(self):
        self.assertIsNone(self.monitor.compactar_banco(self.conn))

    def test_o_arquivo_encolhe_de_verdade(self):
        """Em WAL, VACUUM sem checkpoint deixa o arquivo do mesmo tamanho.

        Pior: o -wal fica do tamanho do banco inteiro, então o pico de disco é o
        dobro — exatamente o oposto do motivo de rodar VACUUM. Este teste falha
        se o `wal_checkpoint(TRUNCATE)` sair.
        """
        self.encher_e_esvaziar()
        antes = self.monitor.DB_PATH.stat().st_size
        with patch.object(self.monitor, "VACUUM_MIN_LIVRE_MB", 1.0):
            recuperado = self.monitor.compactar_banco(self.conn)
        depois = self.monitor.DB_PATH.stat().st_size
        self.assertLess(depois, antes / 2, "o arquivo tinha que encolher no disco")
        self.assertGreater(recuperado, 0)
        wal = self.monitor.DB_PATH.with_name(self.monitor.DB_PATH.name + "-wal")
        if wal.exists():
            self.assertLess(wal.stat().st_size, antes,
                            "o WAL não pode ficar do tamanho do banco antigo")

    def test_banco_segue_utilizavel_depois_de_compactar(self):
        self.monitor.guardar_localizacao(self.conn, 28.4, -81.5, chat_id="777")
        self.encher_e_esvaziar()
        with patch.object(self.monitor, "VACUUM_MIN_LIVRE_MB", 1.0):
            self.monitor.compactar_banco(self.conn)
        self.assertEqual(
            self.monitor.ultima_localizacao(self.conn, chat_id="777"), (28.4, -81.5))
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal",
                         "o VACUUM não pode desfazer o WAL")

    def test_adia_quando_o_disco_nao_cabe_a_copia(self):
        """O VACUUM monta o banco novo ao lado: sem espaço, falha no meio."""
        with patch.object(self.monitor, "paginas_livres_mb", return_value=120.0), \
             patch.object(self.monitor, "espaco_em_disco", return_value=(0.05, 99.0)), \
             patch.object(self.monitor, "DB_PATH", BancoDe(2_000_000_000)):
            self.assertIsNone(self.monitor.compactar_banco(self.conn))

    def test_disco_indisponivel_nao_impede_a_compactacao(self):
        self.encher_e_esvaziar()
        with patch.object(self.monitor, "espaco_em_disco", return_value=None), \
             patch.object(self.monitor, "VACUUM_MIN_LIVRE_MB", 1.0):
            self.assertIsNotNone(self.monitor.compactar_banco(self.conn))

    def test_falha_do_vacuum_nao_derruba_a_manutencao(self):
        with patch.object(self.monitor, "compactar_banco",
                          side_effect=sqlite3.OperationalError("disk I/O error")):
            self.monitor.maybe_maintain_db(self.conn, self.config)  # não pode propagar
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM database_maintenance").fetchone(),
            "a manutenção do dia continua registrada")


if __name__ == "__main__":
    unittest.main()
