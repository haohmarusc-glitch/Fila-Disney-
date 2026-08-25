"""O trocador de bloco do Caddyfile.

Existe porque o Caddyfile é compartilhado com o Premercado. O runbook antigo
mandava `cat >>` o bloco do site: rodar duas vezes cria dois blocos com o mesmo
hostname, o Caddy recusa a configuração inteira e o premercadosc.com cai junto.
Por isso a maioria dos testes aqui não é sobre o bloco novo — é sobre o que
está em volta continuar intacto.
"""
import importlib.util
import pathlib
import unittest

_CAMINHO = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "caddy_bloco.py"
_spec = importlib.util.spec_from_file_location("caddy_bloco", _CAMINHO)
caddy_bloco = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(caddy_bloco)

HOST = "filadisney.premercadosc.com"


def _fechamentos_de_topo(texto):
    """Chaves fechando na coluna 0 — uma por bloco de site.

    Contagem independente do módulo de propósito: se ela usasse o mesmo
    `_sem_ruido`, um erro lá passaria pelos dois lados.
    """
    return sum(1 for linha in texto.splitlines() if linha == "}")

BLOCO_NOVO = """filadisney.premercadosc.com {
    basic_auth {
        familia HASH
    }
    handle /api/* {
        reverse_proxy fila-disney-api:8080 {
            header_up Authorization "Bearer {env.WEB_API_TOKEN}"
        }
    }
    root * /srv/filadisney
    file_server
}"""

PREMERCADO = """premercadosc.com {
    encode gzip
    reverse_proxy premercado:5000
}"""


class TestAcrescenta(unittest.TestCase):
    def test_acrescenta_quando_nao_existe(self):
        saida = caddy_bloco.trocar(PREMERCADO + "\n", HOST, BLOCO_NOVO)
        self.assertIn("premercadosc.com {", saida)
        self.assertIn("root * /srv/filadisney", saida)
        self.assertEqual(saida.count(HOST + " {"), 1)

    def test_arquivo_vazio_vira_so_o_bloco(self):
        self.assertEqual(caddy_bloco.trocar("", HOST, BLOCO_NOVO), BLOCO_NOVO + "\n")


class TestTroca(unittest.TestCase):
    ANTIGO = """filadisney.premercadosc.com {
    root * /srv/filadisney
    file_server
}"""

    def test_troca_no_lugar_sem_duplicar(self):
        original = f"{PREMERCADO}\n\n{self.ANTIGO}\n"
        saida = caddy_bloco.trocar(original, HOST, BLOCO_NOVO)
        self.assertEqual(saida.count(HOST + " {"), 1)
        self.assertIn("basic_auth", saida)

    def test_rodar_duas_vezes_da_o_mesmo_arquivo(self):
        """O ponto do módulo. Com `cat >>` a segunda rodada derrubava o Caddy."""
        uma = caddy_bloco.trocar(f"{PREMERCADO}\n", HOST, BLOCO_NOVO)
        duas = caddy_bloco.trocar(uma, HOST, BLOCO_NOVO)
        self.assertEqual(uma, duas)

    def test_o_que_vem_depois_do_bloco_sobrevive(self):
        depois = "api-filadisney.premercadosc.com {\n    reverse_proxy fila-disney-api:8080\n}"
        original = f"{PREMERCADO}\n\n{self.ANTIGO}\n\n{depois}\n"
        saida = caddy_bloco.trocar(original, HOST, BLOCO_NOVO)
        self.assertIn("api-filadisney.premercadosc.com {", saida)
        self.assertIn("reverse_proxy premercado:5000", saida)

    def test_hostname_da_api_nao_e_confundido_com_o_do_site(self):
        """`api-filadisney...` contém o nome do site; casar por substring apagaria
        o bloco errado."""
        api = "api-filadisney.premercadosc.com {\n    reverse_proxy fila-disney-api:8080\n}"
        saida = caddy_bloco.trocar(api + "\n", HOST, BLOCO_NOVO)
        self.assertIn("api-filadisney.premercadosc.com {", saida)
        self.assertIn("root * /srv/filadisney", saida)


class TestContagemDeChaves(unittest.TestCase):
    def test_comentario_com_chave_nao_fecha_o_bloco_cedo(self):
        """Contar chaves sem tirar o comentário termina o bloco na linha errada.

        O sintoma não é o comentário sobreviver — é a cauda do bloco antigo
        (`root`, e a chave que o fechava) ficar órfã no arquivo, e uma chave
        sobrando faz o Caddy recusar tudo.
        """
        antigo = """filadisney.premercadosc.com {
    # cuidado: uma chave } solta neste comentário
    root * /srv/filadisney
}"""
        original = f"{antigo}\n\n{PREMERCADO}\n"
        saida = caddy_bloco.trocar(original, HOST, BLOCO_NOVO)
        self.assertNotIn("uma chave } solta", saida)
        self.assertIn("reverse_proxy premercado:5000", saida)
        self.assertEqual(_fechamentos_de_topo(saida), 2, saida)

    def test_placeholder_entre_aspas_nao_desequilibra(self):
        """O bloco novo tem `{env.WEB_API_TOKEN}` dentro de aspas."""
        uma = caddy_bloco.trocar(PREMERCADO + "\n", HOST, BLOCO_NOVO)
        duas = caddy_bloco.trocar(uma, HOST, BLOCO_NOVO)
        self.assertEqual(uma.count("premercadosc.com {"), duas.count("premercadosc.com {"))
        self.assertIn("reverse_proxy premercado:5000", duas)

    def test_endereco_multiplo_na_mesma_linha(self):
        antigo = "www.filadisney.premercadosc.com, filadisney.premercadosc.com {\n    file_server\n}"
        saida = caddy_bloco.trocar(antigo + "\n", HOST, BLOCO_NOVO)
        self.assertNotIn("www.filadisney", saida)
        self.assertIn("basic_auth", saida)

    def test_diretiva_indentada_com_o_nome_nao_abre_bloco(self):
        original = """premercadosc.com {
    redir https://filadisney.premercadosc.com {
        status 302
    }
}"""
        saida = caddy_bloco.trocar(original + "\n", HOST, BLOCO_NOVO)
        self.assertIn("redir https://filadisney.premercadosc.com {", saida)
        self.assertIn("root * /srv/filadisney", saida)

    def test_bloco_sem_fechar_e_erro_explicito(self):
        quebrado = "filadisney.premercadosc.com {\n    file_server\n"
        with self.assertRaises(ValueError):
            caddy_bloco.trocar(quebrado, HOST, BLOCO_NOVO)


if __name__ == "__main__":
    unittest.main()
