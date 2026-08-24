"""O coletor de durações do TouringPlans.

O `duracoes.py` é script avulso, como o `coords.py`: roda uma vez e preenche o
`duracoes.json`. O que precisa de teste é o parser e o casamento de nomes — é
onde um erro grava duração errada, ou na atração errada, em silêncio.

A fonte mudou da Wikipédia para o TouringPlans porque as duas medem coisas
diferentes: o infobox traz o ciclo da atração, o TouringPlans traz o total com
pré-show. É o total que responde "cabe antes de fechar".
"""
import sys
import unittest

from tests.apoio import _requests

sys.modules.setdefault("requests", _requests)
import duracoes  # noqa: E402
import monitor  # noqa: E402


PAGINA = """
<html><head><style>.x{color:red}</style></head><body>
<script>var naoDeveAparecer = "1. Fake in NOWHERE";</script>
<h1>Attraction Durations at Magic Kingdom</h1>
<ol>
  <li>1. Space Mountain in TOMORROWLAND<br>Duration: 10 min</li>
  <li>2. Seven Dwarfs Mine Train in FANTASYLAND<br>Duration: 3 min</li>
  <li>3. Bibbidi Bobbidi Boutique in FANTASYLAND<br>Duration: 1 hr</li>
  <li>4. Sonny Eclipse in TOMORROWLAND</li>
</ol>
</body></html>
"""


class TestParserDaPagina(unittest.TestCase):
    def test_le_nome_e_duracao(self):
        lidas = duracoes.duracoes_da_pagina(PAGINA)
        self.assertEqual(lidas["Space Mountain"], 10)
        self.assertEqual(lidas["Seven Dwarfs Mine Train"], 3)

    def test_hora_vira_minuto(self):
        self.assertEqual(duracoes.duracoes_da_pagina(PAGINA)["Bibbidi Bobbidi Boutique"], 60)

    def test_item_sem_duracao_e_descartado(self):
        """Nome sem 'Duration:' embaixo não vira entrada com valor do vizinho."""
        self.assertNotIn("Sonny Eclipse", duracoes.duracoes_da_pagina(PAGINA))

    def test_script_e_style_nao_entram(self):
        self.assertNotIn("Fake", " ".join(duracoes.texto_visivel(PAGINA)))

    def test_pagina_sem_atracao_devolve_vazio(self):
        """Vazio é o sinal de layout mudado; o main transforma isso em erro."""
        self.assertEqual(duracoes.duracoes_da_pagina("<html><body>oi</body></html>"), {})


class TestCasamentoComAWatchlist(unittest.TestCase):
    """O nome do site não é o da watchlist, e errar aqui grava na atração errada."""

    def setUp(self):
        self.config = monitor.load_config()

    def _mapear(self, cruas, parque="Disney Magic Kingdom"):
        return duracoes.mapear_para_watchlist(cruas, self.config["parks"][parque])

    def test_nome_decorado_casa_com_o_canonico(self):
        achadas, _ = self._mapear({"The Haunted Mansion": 10})
        self.assertEqual(achadas["Haunted Mansion"][0], 10)

    def test_o_nome_do_site_vem_junto_para_conferencia(self):
        achadas, _ = self._mapear({"The Haunted Mansion": 10})
        self.assertEqual(achadas["Haunted Mansion"][1], "The Haunted Mansion")

    def test_atracao_fora_da_watchlist_e_ignorada(self):
        achadas, conflitos = self._mapear({"Astro Orbiter": 2, "Mad Tea Party": 2})
        self.assertEqual((achadas, conflitos), ({}, []))

    def test_duas_versoes_que_concordam_entram(self):
        """Mission: SPACE tem Green e Orange, e as duas duram o mesmo."""
        achadas, conflitos = self._mapear(
            {"Mission: SPACE Green": 15, "Mission: SPACE Orange": 15}, "Epcot")
        self.assertEqual(achadas["Mission: SPACE"][0], 15)
        self.assertEqual(conflitos, [])

    def test_duas_versoes_que_divergem_ficam_de_fora(self):
        """Não dá para saber qual o visitante pega — mesma regra das pistas."""
        achadas, conflitos = self._mapear(
            {"Mission: SPACE Green": 12, "Mission: SPACE Orange": 15}, "Epcot")
        self.assertNotIn("Mission: SPACE", achadas)
        self.assertEqual(conflitos[0][0], "Mission: SPACE")

    def test_fila_paralela_nao_entra(self):
        """`nome_watchlist` já barra single rider; a regra 10 vale aqui também."""
        achadas, _ = self._mapear({"Space Mountain Single Rider": 10})
        self.assertEqual(achadas, {})

    def test_todo_parque_do_mapa_existe_na_watchlist(self):
        """Nome errado aqui coletaria a página e jogaria o resultado fora."""
        for parque in duracoes.PAGINAS:
            with self.subTest(parque=parque):
                self.assertIn(parque, self.config["parks"])

    def test_o_mapa_cobre_os_sete_parques(self):
        self.assertEqual(set(duracoes.PAGINAS), set(self.config["parks"]))


class TestBuscaPassaPeloMonitor(unittest.TestCase):
    """Regra 11: a coleta usa o `get_texto`, que tem o retry do `get_json`."""

    def test_coletar_usa_get_texto_e_monta_a_url(self):
        chamadas = []
        original = monitor.get_texto
        self.addCleanup(setattr, monitor, "get_texto", original)

        def falso_get_texto(url, *, tentativas=3):
            chamadas.append(url)
            return PAGINA

        monitor.get_texto = falso_get_texto
        lidas = duracoes.coletar("Disney Magic Kingdom", "/magic-kingdom/attractions/duration")
        self.assertEqual(chamadas, ["https://touringplans.com/magic-kingdom/attractions/duration"])
        self.assertIn("Space Mountain", lidas)
