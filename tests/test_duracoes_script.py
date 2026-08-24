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


# Estrutura COPIADA da página real, colhida na VPS em 24/08/2026: rótulo e
# valor em linhas separadas, área em linha própria, e nota de avaliação e
# considerações físicas no meio. A primeira versão do parser foi escrita contra
# uma estrutura imaginada e voltou zero nos sete parques — este fixture existe
# para que isso não se repita em silêncio.
PAGINA = """
<html><head><style>.x{color:red}</style></head><body>
<script>var naoDeveAparecer = "9. Fake";</script>
<h1>Attraction Durations at Magic Kingdom</h1>
<div>1. Space Mountain</div>
<div>in Tomorrowland</div>
<div>(4.1/5 &middot; 9,001 reviews)</div>
<div>Physical Considerations</div><div>;</div>
<div>Must Transfer From Wheelchair/ECV</div>
<div>Duration:</div>
<div>10 min</div>

<div>2. Seven Dwarfs Mine Train</div>
<div>in Fantasyland</div>
<div>Duration:</div>
<div>3 min</div>

<div>3. Bibbidi Bobbidi Boutique</div>
<div>in Fantasyland</div>
<div>Not Enough User Ratings</div>
<div>Duration:</div>
<div>1 hr</div>

<div>4. The Haunted Mansion</div>
<div>in Liberty Square</div>
<div>Duration:</div>
<div>10 min</div>

<div>5. Astro Orbiter</div>
<div>in Tomorrowland</div>
<div>Duration:</div>
<div>2 min</div>

<div>6. Sonny Eclipse</div>
<div>in Tomorrowland</div>
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

    def test_rotulo_e_valor_em_linhas_separadas(self):
        """É assim que a página real escreve, e foi o que derrubou a v1.

        `'Duration:'` sozinho não tem número; `'7 min'` sozinho não tem rótulo.
        Casar só a forma grudada devolvia ZERO nos sete parques.
        """
        linhas = ["1. Big Thunder Mountain Railroad", "in Frontierland",
                  "Duration:", "7 min"]
        html = "".join(f"<p>{l}</p>" for l in linhas)
        self.assertEqual(duracoes.duracoes_da_pagina(html),
                         {"Big Thunder Mountain Railroad": 7})

    def test_forma_grudada_tambem_serve(self):
        """Não é a forma de hoje, mas aceitar as duas custa uma linha."""
        html = "<p>1. Space Mountain</p><p>Duration: 10 min</p>"
        self.assertEqual(duracoes.duracoes_da_pagina(html), {"Space Mountain": 10})

    def test_rotulo_sem_valor_logo_abaixo_nao_inventa(self):
        html = "<p>1. Space Mountain</p><p>Duration:</p><p>Reserve on the app</p>"
        self.assertEqual(duracoes.duracoes_da_pagina(html), {})

    def test_area_e_avaliacao_no_meio_nao_atrapalham(self):
        self.assertEqual(duracoes.duracoes_da_pagina(PAGINA)["The Haunted Mansion"], 10)

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

    def test_pavilhao_nao_disputa_com_a_atracao(self):
        """Caso real: o Test Track ficou sem duração por um empate falso.

        "Test Track Pavilion" é o prédio, e os 2 min são de atravessá-lo. A
        atração é "Test Track presented by General Motors". Sem separar os dois,
        o conflito derrubava a atração inteira.
        """
        achadas, conflitos = self._mapear(
            {"Test Track Pavilion": 2,
             "Test Track presented by General Motors": 4}, "Epcot")
        self.assertEqual(achadas["Test Track"][0], 4)
        self.assertEqual(conflitos, [])

    def test_pavilhao_sozinho_tambem_nao_entra(self):
        achadas, _ = self._mapear({"Test Track Pavilion": 2}, "Epcot")
        self.assertEqual(achadas, {})

    def test_atracao_com_nome_parecido_nao_e_confundida_com_pavilhao(self):
        self.assertFalse(duracoes.e_pavilhao("Test Track presented by General Motors"))
        self.assertTrue(duracoes.e_pavilhao("Test Track Pavilion"))
        self.assertTrue(duracoes.e_pavilhao("  Japan Pavilion  "))

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


class TestContagemDeReferencia(unittest.TestCase):
    """Página com bem menos atrações que o medido é parser quebrado, não parque.

    A v1 devolveu ZERO nos sete parques e o CI estava verde. A contagem de
    referência é o alarme que teria pego: 0 de 76 não é flutuação sazonal.
    """

    def test_toda_pagina_tem_referencia(self):
        self.assertEqual(set(duracoes.CONTAGENS_REFERENCIA), set(duracoes.PAGINAS))

    def test_referencias_batem_com_o_medido(self):
        """Colhido na VPS em 24/08/2026. Mudou muito? Medir de novo, não afrouxar."""
        self.assertEqual(duracoes.CONTAGENS_REFERENCIA["Disney Magic Kingdom"], 76)
        self.assertEqual(duracoes.CONTAGENS_REFERENCIA["Epcot"], 94)
        self.assertEqual(duracoes.CONTAGENS_REFERENCIA["Universal Epic Universe"], 19)


class TestAjustesManuais(unittest.TestCase):
    """Correção verificada à mão sobrevive à recoleta e declara proveniência."""

    def test_todo_ajuste_tem_minutos_fonte_e_atracao_da_watchlist(self):
        import json
        import monitor as m
        dados = json.load(open("duracoes.json"))
        watchlist = m.load_config()["parks"]
        ajustes = dados.get("_ajustes", {})
        self.assertTrue(ajustes, "o Rise de 18 min tem que estar aqui")
        for parque, itens in ajustes.items():
            for atracao, ajuste in itens.items():
                with self.subTest(atracao=atracao):
                    self.assertIn(atracao, watchlist[parque]["attractions"])
                    self.assertIsInstance(ajuste["minutos"], int)
                    self.assertGreater(ajuste["minutos"], 0)
                    self.assertTrue(ajuste["fonte"].strip(),
                                    "ajuste sem proveniência é chute com outro nome")

    def test_o_ajuste_esta_aplicado_no_dado_vigente(self):
        """O _ajustes documenta; o rides é o que o bot lê. Os dois têm que bater."""
        import json
        dados = json.load(open("duracoes.json"))
        for parque, itens in dados.get("_ajustes", {}).items():
            for atracao, ajuste in itens.items():
                with self.subTest(atracao=atracao):
                    self.assertEqual(dados["rides"][parque][atracao], ajuste["minutos"])
