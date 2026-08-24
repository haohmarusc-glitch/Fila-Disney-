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
