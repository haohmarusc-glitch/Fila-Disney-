"""O menu de comandos do Telegram (o botão "Menu") sai do /help.

Estes testes guardam duas coisas: que o menu continua saindo de uma lista só,
e que o que NÃO pode ser disparado por um toque continua fora dele.
"""
import re
import unittest
from pathlib import Path

from tests import apoio


class TestMenuDerivadoDoHelp(apoio.BaseTeste):
    def test_menu_sai_do_help(self):
        nomes = [n for n, _ in self.monitor.comandos_do_menu()]
        self.assertEqual(len(nomes), len(set(nomes)), "comando repetido no menu")
        for esperado in ("status", "menores", "fila", "perto", "parques", "help"):
            self.assertIn(esperado, nomes)

    def test_vale_a_primeira_linha_do_comando_nao_a_ultima(self):
        """O /help descreve /status duas vezes; a forma pelada é a de cima.

        Contar nomes não serve de teste aqui: a derivação usa um dict, que
        deduplica sozinho — sem esta asserção, trocar a primeira linha pela
        última passaria batido e o menu prometeria "um parque específico" para
        um toque que não manda parque nenhum.
        """
        descricoes = dict(self.monitor.comandos_do_menu())
        self.assertEqual(descricoes["status"], "fila atual da watchlist do parque de hoje")
        self.assertEqual(descricoes["ranking"], "maiores filas agora em todos os parques")

    def test_ordem_do_help_e_preservada(self):
        nomes = [n for n, _ in self.monitor.comandos_do_menu()]
        posicoes = [self.monitor.HELP.index(f"/{n} ") for n in nomes]
        self.assertEqual(posicoes, sorted(posicoes))

    def test_comando_novo_no_help_entra_no_menu_sozinho(self):
        """O ponto da derivação: ninguém precisa lembrar de mexer em duas listas."""
        help_maior = self.monitor.HELP.replace(
            "/parques — parques monitorados",
            "/parques — parques monitorados\n/inventado — comando de teste",
        )
        nomes = [n for n, _ in self.monitor.comandos_do_menu(help_maior)]
        self.assertIn("inventado", nomes)

    def test_descricao_sai_limpa_de_html(self):
        for nome, descricao in self.monitor.comandos_do_menu():
            self.assertNotIn("<", descricao, nome)
            self.assertNotIn("&lt;", descricao, nome)
            self.assertNotIn("&amp;", descricao, nome)
            self.assertTrue(descricao.strip(), nome)
            self.assertLessEqual(len(descricao), self.notifier.DESCRICAO_MENU_MAX, nome)

    def test_nomes_cabem_no_formato_do_telegram(self):
        for nome, _ in self.monitor.comandos_do_menu():
            self.assertRegex(nome, r"^[a-z0-9_]{1,32}$")

    def test_escrita_e_administracao_ficam_fora(self):
        """Toque no menu dispara na hora, sem confirmação e sem argumento."""
        nomes = {n for n, _ in self.monitor.comandos_do_menu()}
        for proibido in ("teste_alertas", "teste_park_to_park", "entrar", "sair", "revogar"):
            self.assertNotIn(proibido, nomes)
            # mas continuam documentados para quem digita
            self.assertIn(f"/{proibido}", self.monitor.HELP)

    def test_forma_pelada_de_cada_item_do_menu_responde(self):
        """Tocar no menu manda o comando sem argumento: nenhum pode ficar mudo."""
        park_ids = {"Disney Magic Kingdom": 6}
        sem_argumento = {"perto", "personagens_perto"}  # respondem pedindo localização
        for nome, _ in self.monitor.comandos_do_menu():
            if nome in sem_argumento:
                continue
            resposta = self.monitor.handle_command(
                f"/{nome}", self.conn, self.config, park_ids, {}, apoio.CHAT_FAKE)
            self.assertTrue(resposta, f"/{nome} não respondeu nada")


class TestPublicacaoDoMenu(apoio.BaseTeste):
    def test_publica_no_endpoint_do_telegram(self):
        urls = []
        self.requests.roteador_post = lambda url, payload: (
            urls.append(url) or apoio.Resposta({"ok": True}))
        self.assertTrue(self.notifier.set_my_commands([("status", "fila agora")]))
        self.assertTrue(urls[0].endswith("/setMyCommands"))
        self.assertEqual(self.requests.posts[0],
                         {"commands": [{"command": "status", "description": "fila agora"}]})

    def test_barra_inicial_e_descricao_com_quebra_sao_normalizadas(self):
        self.notifier.set_my_commands([("/status", "fila\n  agora  ")])
        self.assertEqual(self.requests.posts[0]["commands"],
                         [{"command": "status", "description": "fila agora"}])

    def test_item_invalido_e_descartado_sem_derrubar_o_resto(self):
        """O Telegram recusa a chamada INTEIRA por um item fora do formato."""
        ok = self.notifier.set_my_commands([
            ("Status", "maiúscula não passa"),
            ("com-hifen", "hífen não passa"),
            ("x" * 33, "nome longo demais"),
            ("vazio", "   "),
            ("fila", "essa vale"),
        ])
        self.assertTrue(ok)
        self.assertEqual(self.requests.posts[0]["commands"],
                         [{"command": "fila", "description": "essa vale"}])

    def test_descricao_longa_e_cortada(self):
        self.notifier.set_my_commands([("fila", "x" * 400)])
        enviada = self.requests.posts[0]["commands"][0]["description"]
        self.assertEqual(len(enviada), self.notifier.DESCRICAO_MENU_MAX)

    def test_nada_valido_nao_vira_chamada(self):
        self.assertFalse(self.notifier.set_my_commands([("NAO", "vale")]))
        self.assertEqual(self.requests.posts, [])

    def test_http_ruim_devolve_false_sem_explodir(self):
        self.requests.roteador_post = lambda url, payload: apoio.Resposta(
            None, status=400, texto="Bad Request: too many commands")
        self.assertFalse(self.notifier.set_my_commands([("fila", "fila agora")]))

    def test_rede_fora_devolve_false_sem_explodir(self):
        def cai(url, payload):
            raise self.requests.RequestException("sem rede")
        self.requests.roteador_post = cai
        self.assertFalse(self.notifier.set_my_commands([("fila", "fila agora")]))

    def test_sem_token_nao_chama(self):
        self.notifier.BOT_TOKEN = ""
        self.assertFalse(self.notifier.set_my_commands([("fila", "fila agora")]))
        self.assertEqual(self.requests.posts, [])


class TestHelpCobreODispatch(unittest.TestCase):
    """Comando atendido e não documentado não aparece no /help nem no menu."""

    # /start, /ajuda e /agora são apelidos de comandos que o /help já lista
    APELIDOS = {"/start", "/ajuda", "/agora"}

    def test_todo_comando_atendido_esta_no_help(self):
        fonte = Path(__file__).resolve().parent.parent / "monitor.py"
        texto = fonte.read_text()
        atendidos = set(re.findall(r'cmd == "(/\w+)"', texto))
        for grupo in re.findall(r'cmd in \(([^)]*)\)', texto):
            atendidos.update(re.findall(r'"(/\w+)"', grupo))
        atendidos.update(re.findall(r'comando == "(/\w+)"', texto))
        self.assertTrue(atendidos, "não achei o dispatch — o regex envelheceu")

        import monitor
        for cmd in sorted(atendidos - self.APELIDOS):
            self.assertIn(f"{cmd} ", monitor.HELP, f"{cmd} é atendido e não está no /help")


if __name__ == "__main__":
    unittest.main()
