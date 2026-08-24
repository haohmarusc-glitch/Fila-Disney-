"""O extrator de duração da Wikipédia.

O `duracoes.py` é script avulso, como o `coords.py`: roda uma vez e preenche o
`duracoes.json`. O que precisa de teste é o parser — é ele que decide se um
número entra no arquivo ou não, e um erro aqui grava duração errada em silêncio.
"""
import sys
import unittest

from tests.apoio import _requests

sys.modules.setdefault("requests", _requests)
import duracoes  # noqa: E402


class TestParserDeDuracao(unittest.TestCase):
    def test_formatos_que_aparecem_no_infobox(self):
        casos = {
            "3 minutes": 3,
            "22 minutes": 22,
            "1 minute": 1,
            "Approximately 8 minutes": 8,
            "2:30": 3,
            "1 minute, 30 seconds": 2,
            "4 minutes 30 seconds": 5,
            "4 minutes 25 seconds": 4,
            "90 seconds": 2,
            "45 seconds": 1,
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(duracoes.minutos_do_texto(texto), esperado)

    def test_marcacao_de_wiki_nao_atrapalha(self):
        for texto in ("{{convert|3|min}}", "{{nowrap|3 minutes}}",
                      "'''3 minutes'''", "[[3 minutes]]", "<small>3 minutes</small>"):
            with self.subTest(texto=texto):
                self.assertEqual(duracoes.minutos_do_texto(texto), 3)

    def test_meio_a_meio_arredonda_para_cima(self):
        """round() do Python é bancário: round(2.5) devolve 2, e 2:30 viraria 2 min.

        Subestimar o compromisso de tempo é o erro que atrapalha quem decide se
        cabe antes de fechar.
        """
        self.assertEqual(duracoes.minutos_do_texto("2:30"), 3)
        self.assertEqual(duracoes.minutos_do_texto("4 minutes 30 seconds"), 5)

    def test_o_que_nao_da_para_ler_vira_None(self):
        """Regra 12: sem dado reconhecido, sem estimativa."""
        for texto in ("", "varies", "0", "unknown", "several minutes", None):
            with self.subTest(texto=texto):
                self.assertIsNone(duracoes.minutos_do_texto(texto))

    def test_nunca_devolve_zero(self):
        """Duração 0 na tela seria dado inventado disfarçado."""
        for texto in ("1 second", "0 minutes", "10 seconds"):
            with self.subTest(texto=texto):
                minutos = duracoes.minutos_do_texto(texto)
                self.assertTrue(minutos is None or minutos >= 1)


class TestChamadaHTTP(unittest.TestCase):
    """O caminho de rede, que a primeira versão nunca exercitou.

    `monitor.get_json(url, *, tentativas)` não aceita `params`, e o script
    chamava com `params={...}`. Resultado: TypeError nas 54 atrações, antes de
    tocar a rede — e o `except` largo rotulava como "falha de rede". O parser
    tinha 16 casos de teste; a chamada não tinha nenhum.

    O falso abaixo repete a assinatura REAL de propósito. Um Mock aceitaria
    qualquer argumento e deixaria o bug passar de novo.
    """

    def setUp(self):
        self.chamadas = []
        self.resposta = {}
        self.original = duracoes.monitor.get_json
        self.addCleanup(setattr, duracoes.monitor, "get_json", self.original)

        def falso_get_json(url, *, tentativas=3):
            self.chamadas.append(url)
            return self.resposta

        duracoes.monitor.get_json = falso_get_json

    def test_busca_monta_a_query_na_url(self):
        self.resposta = {"query": {"search": [{"title": "Space Mountain"}]}}
        self.assertEqual(duracoes.buscar_pagina("Space Mountain"), "Space Mountain")
        url = self.chamadas[0]
        self.assertIn("action=query", url)
        self.assertIn("list=search", url)
        self.assertIn("format=json", url)

    def test_titulo_que_nao_casa_e_recusado(self):
        """Buscar 'Tower of Terror' pode devolver o filme, ou a versão de Paris."""
        self.resposta = {"query": {"search": [{"title": "The Twilight Zone (film)"}]}}
        self.assertIsNone(duracoes.buscar_pagina("Tower of Terror"))

    def test_titulo_decorado_ainda_casa(self):
        self.resposta = {"query": {"search": [
            {"title": "The Twilight Zone Tower of Terror"}]}}
        self.assertEqual(duracoes.buscar_pagina("Tower of Terror"),
                         "The Twilight Zone Tower of Terror")

    def test_le_a_duracao_da_pagina(self):
        self.resposta = {"query": {"pages": {"1": {"revisions": [
            {"slots": {"main": {"*": "{{Infobox\n| duration = 3 minutes\n}}"}}}]}}}}
        self.assertEqual(duracoes.duracao_da_pagina("Space Mountain"), 3)

    def test_pagina_sem_revisao_nao_quebra(self):
        self.resposta = {"query": {"pages": {"-1": {"missing": ""}}}}
        self.assertIsNone(duracoes.duracao_da_pagina("Inexistente"))

    def test_resposta_vazia_nao_quebra(self):
        self.resposta = {}
        self.assertIsNone(duracoes.buscar_pagina("Qualquer"))
        self.assertIsNone(duracoes.duracao_da_pagina("Qualquer"))


class TestArtigoDeVariosParques(unittest.TestCase):
    """Atração que existe em vários resorts tem UM artigo cobrindo todos.

    Medido em 24/08/2026: o artigo do Pirates devolveu 16 min, que é o da
    Disneyland — o do Magic Kingdom é bem mais curto. Duração do parque errado é
    pior que duração nenhuma: parece certa, ninguém desconfia, e entra na conta
    de "cabe antes de fechar".
    """

    MULTI = """{{Infobox attraction
| name = Pirates of the Caribbean
| park = Disneyland Park
| park2 = Magic Kingdom
| duration = 15:30
}}"""
    UNICO = """{{Infobox attraction
| name = Test Track
| park = Epcot
| duration = 5 minutes
}}"""

    def setUp(self):
        self.original = duracoes.monitor.get_json
        self.addCleanup(setattr, duracoes.monitor, "get_json", self.original)

    def responder(self, wikitexto):
        def falso(url, *, tentativas=3):
            return {"query": {"pages": {"1": {"revisions": [
                {"slots": {"main": {"*": wikitexto}}}]}}}}
        duracoes.monitor.get_json = falso

    def test_dois_resorts_no_infobox_recusam_a_duracao(self):
        self.responder(self.MULTI)
        with self.assertRaises(duracoes.Ambigua):
            duracoes.duracao_da_pagina("Pirates of the Caribbean (attraction)")

    def test_um_resort_passa(self):
        self.responder(self.UNICO)
        self.assertEqual(duracoes.duracao_da_pagina("Test Track"), 5)

    def test_a_excecao_diz_quais_parques(self):
        self.responder(self.MULTI)
        try:
            duracoes.duracao_da_pagina("Pirates of the Caribbean (attraction)")
        except duracoes.Ambigua as exc:
            self.assertIn("disneyland park", exc.args[0])
            self.assertIn("magic kingdom", exc.args[0])
        else:
            self.fail("devia ter recusado")

    def test_so_olha_o_infobox_nao_o_artigo_inteiro(self):
        """O corpo do texto cita outros parques o tempo todo; o infobox, não."""
        texto = self.UNICO + "\n\nUma atração parecida existe na Disneyland Paris."
        self.responder(texto)
        self.assertEqual(duracoes.duracao_da_pagina("Test Track"), 5)


class TestCampoDuration(unittest.TestCase):
    INFOBOX = """{{Infobox attraction
| name = Space Mountain
| status = Operating
| duration = 2 minutes, 35 seconds
| restriction_in = 44
}}"""

    def test_extrai_parando_na_proxima_chave(self):
        self.assertEqual(duracoes.campo_duration(self.INFOBOX).strip(),
                         "2 minutes, 35 seconds")

    def test_infobox_sem_duration(self):
        self.assertIsNone(duracoes.campo_duration("{{Infobox\n| name = X\n}}"))

    def test_ponta_a_ponta_do_infobox_ao_numero(self):
        self.assertEqual(
            duracoes.minutos_do_texto(duracoes.campo_duration(self.INFOBOX)), 3)


if __name__ == "__main__":
    unittest.main()
