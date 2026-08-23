"""/grupo: onde está a família, entre quem escolheu compartilhar.

É posição de gente real num chat, então o desenho é conservador: opt-in
explícito, ver exige compartilhar, sai parque e referência em vez de lat/lon, e
posição velha simplesmente não aparece.
"""
import datetime as dt
import json
import unittest
from unittest.mock import patch

from tests.apoio import CHAT_FAKE, RAIZ, BaseTeste

# O coords.json de verdade: o BaseTeste redireciona o caminho para um tmp, e um
# fixture inventado não provaria que o /grupo funciona com as coordenadas que
# estão em produção.
COORDS = json.loads((RAIZ / "coords.json").read_text(encoding="utf-8"))
EPCOT = tuple(COORDS["parks"]["Epcot"])
TEST_TRACK = tuple(COORDS["rides"]["Epcot"]["Test Track"])


class BaseGrupo(BaseTeste):
    def setUp(self):
        super().setUp()
        self.coords = COORDS
        self.eu, self.outro = CHAT_FAKE, "555"
        self.autorizar(self.outro)

    def autorizar(self, chat_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO authorized_chats (chat_id, authorized_at) VALUES (?, ?)",
            (str(chat_id), self.monitor.utc_now().isoformat()))
        self.conn.commit()

    def entrar(self, chat_id, nome=None):
        self.monitor.definir_compartilhamento(self.conn, chat_id, True)
        if nome:
            self.monitor.registrar_nome_chat(self.conn, chat_id, nome)

    def posicionar(self, chat_id, coord=TEST_TRACK, minutos=0):
        self.monitor.guardar_localizacao(self.conn, coord[0], coord[1], chat_id)
        if minutos:
            quando = (self.monitor.utc_now() - dt.timedelta(minutes=minutos)).isoformat()
            self.conn.execute("UPDATE user_locations SET updated_at = ? WHERE chat_id = ?",
                              (quando, str(chat_id)))
            self.conn.commit()

    def grupo(self, chat_id=None):
        return self.monitor.format_grupo(
            self.conn, self.config, self.coords, chat_id or self.eu)


class TestOptIn(BaseGrupo):
    def test_ninguem_compartilha_por_padrao(self):
        self.assertFalse(self.monitor.compartilha_no_grupo(self.conn, self.eu))

    def test_ver_exige_compartilhar(self):
        """Sem simetria o comando vira janela de mão única para quem não se expõe."""
        self.entrar(self.outro, "Pedro")
        self.posicionar(self.outro)
        texto = self.grupo()
        self.assertIn("para ver é preciso compartilhar", texto)
        self.assertNotIn("Pedro", texto)

    def test_entrar_e_sair(self):
        self.monitor.definir_compartilhamento(self.conn, self.eu, True)
        self.assertTrue(self.monitor.compartilha_no_grupo(self.conn, self.eu))
        self.monitor.definir_compartilhamento(self.conn, self.eu, False)
        self.assertFalse(self.monitor.compartilha_no_grupo(self.conn, self.eu))

    def test_quem_saiu_some_para_os_outros(self):
        self.entrar(self.eu, "Eu")
        self.entrar(self.outro, "Pedro")
        self.posicionar(self.outro)
        self.assertIn("Pedro", self.grupo())
        self.monitor.definir_compartilhamento(self.conn, self.outro, False)
        self.assertNotIn("Pedro", self.grupo())


class TestConteudo(BaseGrupo):
    def setUp(self):
        super().setUp()
        self.entrar(self.eu, "Eu")
        self.entrar(self.outro, "Pedro")

    def test_mostra_parque_e_referencia(self):
        self.posicionar(self.outro)
        texto = self.grupo()
        self.assertIn("Pedro", texto)
        self.assertIn("Epcot", texto)
        self.assertIn("perto de Test Track", texto)

    def test_nao_publica_coordenada_crua(self):
        """Responder 'onde todo mundo está' não exige publicar lat/lon no chat."""
        self.posicionar(self.outro)
        texto = self.grupo()
        for numero in ("28.37", "-81.54"):
            self.assertNotIn(numero, texto)

    def test_distancia_so_com_a_minha_posicao(self):
        self.posicionar(self.outro)
        self.assertNotIn("de você", self.grupo())
        self.posicionar(self.eu, EPCOT)
        self.assertIn("de você", self.grupo())

    def test_diz_ha_quanto_tempo(self):
        self.posicionar(self.outro, minutos=25)
        self.assertIn("há 25 min", self.grupo())

    def test_posicao_velha_nao_aparece(self):
        self.posicionar(self.outro, minutos=self.monitor.GRUPO_MAX_MINUTOS + 10)
        self.assertIn("Ninguém mais compartilhou", self.grupo())

    def test_nao_me_lista_para_mim_mesmo(self):
        self.posicionar(self.eu)
        self.assertIn("Ninguém mais compartilhou", self.grupo())

    def test_referencia_distante_e_omitida(self):
        """'perto de Test Track' longe de Test Track manda o grupo errado.

        Dentro de um parque quase sempre há atração a menos de 400 m — por isso
        o corte é exercitado apertando o raio, e não procurando um ponto vazio
        que só existiria fora do parque.
        """
        self.posicionar(self.outro, EPCOT)  # 257 m da atração mais próxima
        with patch.object(self.monitor, "GRUPO_REFERENCIA_MAX_METROS", 100):
            texto = self.grupo()
        self.assertIn("Epcot", texto, "o parque continua saindo")
        self.assertNotIn("perto de", texto)

    def test_referencia_vem_com_a_distancia_medida(self):
        nome, metros = self.monitor.atracao_mais_proxima(TEST_TRACK, "Epcot", COORDS)
        self.assertEqual(nome, "Test Track")
        self.assertLess(metros, 5)

    def test_parque_sem_coordenada_nao_inventa_referencia(self):
        """Regra 12: sem coordenada real, nada de estimativa."""
        self.assertIsNone(
            self.monitor.atracao_mais_proxima(TEST_TRACK, "Parque Inexistente", COORDS))

    def test_fora_dos_parques_e_dito_sem_inventar_referencia(self):
        self.posicionar(self.outro, (28.0, -82.5))  # bem longe
        texto = self.grupo()
        self.assertIn("fora dos parques", texto)
        self.assertNotIn("perto de", texto)

    def test_sem_nome_cai_no_id_do_chat(self):
        self.monitor.definir_compartilhamento(self.conn, "777", True)
        self.autorizar("777")
        self.posicionar("777")
        self.assertIn("chat 777", self.grupo())


class TestAcessoERevogacao(BaseGrupo):
    def test_chat_sem_acesso_nao_entra_no_grupo(self):
        """Compartilhar não pode sobreviver à perda de acesso."""
        self.entrar(self.eu, "Eu")
        self.entrar("999", "Estranho")
        self.posicionar("999")  # nunca esteve em authorized_chats
        self.assertIn("Ninguém mais compartilhou", self.grupo())

    def test_revogar_apaga_a_posicao_junto(self):
        self.entrar(self.outro, "Pedro")
        self.posicionar(self.outro)
        self.monitor.revogar_acesso(self.conn, self.outro, self.eu)
        for tabela in ("group_sharing", "chat_names", "user_locations"):
            with self.subTest(tabela=tabela):
                self.assertIsNone(self.conn.execute(
                    f"SELECT 1 FROM {tabela} WHERE chat_id = ?", (self.outro,)).fetchone())

    def test_manutencao_expira_a_posicao_compartilhada(self):
        self.entrar(self.outro, "Pedro")
        self.posicionar(self.outro, minutos=60 * 24 * 30)
        self.monitor.maybe_maintain_db(self.conn, self.config)
        self.entrar(self.eu, "Eu")
        self.assertIn("Ninguém mais compartilhou", self.grupo())


class TestComando(BaseGrupo):
    def cmd(self, texto, chat_id=None):
        return self.monitor.handle_command(
            texto, self.conn, self.config, {}, self.coords, chat_id or self.eu)

    def test_liga_desliga_e_mostra(self):
        self.assertIn("passa a aparecer", self.cmd("/grupo on"))
        self.assertTrue(self.monitor.compartilha_no_grupo(self.conn, self.eu))
        self.assertIn("Grupo", self.cmd("/grupo"))
        self.assertIn("Você saiu", self.cmd("/grupo off"))
        self.assertFalse(self.monitor.compartilha_no_grupo(self.conn, self.eu))

    def test_esta_no_help(self):
        self.assertIn("/grupo", self.monitor.HELP)

    def test_nao_chama_a_api(self):
        antes = len(self.requests.gets)
        self.cmd("/grupo on")
        self.cmd("/grupo")
        self.assertEqual(len(self.requests.gets), antes)


if __name__ == "__main__":
    unittest.main()
