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
        self.assertEqual(duracoes.duracao_da_pagina("Space Mountain", "Disney Magic Kingdom"), 3)

    def test_pagina_sem_revisao_nao_quebra(self):
        self.resposta = {"query": {"pages": {"-1": {"missing": ""}}}}
        self.assertIsNone(duracoes.duracao_da_pagina("Inexistente", "Epcot"))

    def test_resposta_vazia_nao_quebra(self):
        self.resposta = {}
        self.assertIsNone(duracoes.buscar_pagina("Qualquer"))
        self.assertIsNone(duracoes.duracao_da_pagina("Qualquer", "Epcot"))


class TestArtigoDeVariosParques(unittest.TestCase):
    """Atração que existe em vários resorts tem UM artigo cobrindo todos.

    Contar parques e desistir era conservador demais: descartava clone de
    duração idêntica — Rise of the Resistance, Rock 'n' Roller Coaster, Seven
    Dwarfs Mine Train — junto com o caso legítimo do Pirates. Na execução real
    de 24/08/2026 isso derrubou o resultado de 34 para 15.

    O infobox numera as instalações, então dá para pegar a duração DO NOSSO
    parque. Só quando ela não existe é que a atração fica sem dado.
    """

    MULTI = """{{Infobox attraction
| name = Pirates of the Caribbean
| park = Disneyland Park
| duration = 15:30
| park2 = Magic Kingdom
| duration2 = 8:30
}}"""
    SEM_PROPRIA = """{{Infobox attraction
| name = Pirates of the Caribbean
| park = Disneyland Park
| duration = 15:30
| park2 = Magic Kingdom
| section2 = Adventureland
}}"""
    UNICO = """{{Infobox attraction
| name = Test Track
| park = Epcot
| duration = 5 minutes
}}"""

    def test_pega_a_duracao_do_nosso_parque(self):
        self.assertEqual(
            duracoes.duracao_para_o_parque(self.MULTI, "Disney Magic Kingdom"), 9)

    def test_nao_pega_a_duracao_do_parque_errado(self):
        """15:30 é a Disneyland. Cair nela seria quase o dobro do certo."""
        self.assertNotEqual(
            duracoes.duracao_para_o_parque(self.MULTI, "Disney Magic Kingdom"), 16)

    def test_parque_unico_usa_a_duracao_solta(self):
        self.assertEqual(duracoes.duracao_para_o_parque(self.UNICO, "Epcot"), 5)

    def test_sem_duracao_propria_recusa(self):
        """A duração solta é do artigo inteiro e pode ser de outra instalação."""
        with self.assertRaises(duracoes.Ambigua):
            duracoes.duracao_para_o_parque(self.SEM_PROPRIA, "Disney Magic Kingdom")

    def test_nosso_parque_fora_do_artigo_recusa(self):
        with self.assertRaises(duracoes.Ambigua):
            duracoes.duracao_para_o_parque(self.MULTI, "Epcot")

    def test_a_excecao_diz_quais_instalacoes(self):
        try:
            duracoes.duracao_para_o_parque(self.MULTI, "Epcot")
        except duracoes.Ambigua as exc:
            self.assertIn("magic kingdom", exc.args[0])
            self.assertIn("disneyland park", exc.args[0])
        else:
            self.fail("devia ter recusado")

    def test_todo_parque_da_watchlist_tem_nome_da_wikipedia(self):
        """Faltar um aqui faz o parque inteiro cair em Ambigua sem motivo."""
        import json
        import pathlib
        raiz = pathlib.Path(__file__).resolve().parent.parent
        watchlist = json.loads((raiz / "watchlist.json").read_text(encoding="utf-8"))
        self.assertEqual(set(duracoes.PARQUES_WIKI), set(watchlist["parks"]))


class TestUmParqueUsaOTextoInteiro(unittest.TestCase):
    """Sem segunda instalação não há ambiguidade, então o texto todo vale.

    Trocar a varredura por um parser de infobox fez `Na'vi River Journey` e
    `Kali River Rapids` perderem a duração que já tinham — regressão medida na
    execução de 24/08/2026, quando as duas passaram de `+ 6 min` e `+ 5 min`
    para "sem infobox duration".
    """

    def test_infobox_em_minusculo(self):
        """A Wikipédia aceita {{infobox}} e {{Infobox}}; o find era sensível."""
        texto = ("{{infobox attraction\n| park = Disney's Animal Kingdom\n"
                 "| duration = 6 minutes\n}}")
        self.assertEqual(
            duracoes.duracao_para_o_parque(texto, "Disney Animal Kingdom"), 6)

    def test_duration_fora_do_primeiro_infobox(self):
        texto = ("{{Infobox film\n| name = X\n}}\n"
                 "{{Infobox attraction\n| park = Epcot\n| duration = 5 minutes\n}}")
        self.assertEqual(duracoes.duracao_para_o_parque(texto, "Epcot"), 5)

    def test_duration_como_ultimo_campo(self):
        """Sem o fim-de-texto no lookahead, o último campo do infobox sumia."""
        self.assertEqual(
            duracoes.campo_duration("{{Infobox\n| name = X\n| duration = 4 minutes"),
            "4 minutes")

    def test_a_rede_nao_vale_para_artigo_de_varios_parques(self):
        """O texto inteiro só entra quando não há como confundir de parque."""
        texto = ("{{Infobox attraction\n| park = Disneyland Park\n"
                 "| duration = 15:30\n| park2 = Magic Kingdom\n}}")
        with self.assertRaises(duracoes.Ambigua):
            duracoes.duracao_para_o_parque(texto, "Disney Magic Kingdom")


class TestRedeIndependenteDoParser(unittest.TestCase):
    """A checagem por NOME existe porque o parser pode falhar em separar.

    Regressão medida em 24/08/2026: a rede "uma instalação, sem ambiguidade"
    trouxe o Pirates de volta com 16 min — o número da Disneyland. "Uma
    instalação" significa duas coisas diferentes: artigo de um parque só, ou
    parser que não enxergou as chaves `park2`. Só a primeira é segura.
    """

    SEM_CHAVES = """{{Infobox attraction
| name = Pirates of the Caribbean
| location = Disneyland Park, Magic Kingdom, Tokyo Disneyland
| duration = 15:30
}}"""
    COM_CHAVES = """{{Infobox attraction
| park = Disneyland Park
| duration = 15:30
| park2 = Magic Kingdom
| duration2 = 8:30
}}"""
    UM_PARQUE = """{{infobox attraction
| park = Disney's Animal Kingdom
| duration = 6 minutes
}}"""

    def test_varios_resorts_sem_chave_park2_e_recusado(self):
        """Sem as chaves numeradas, a duração solta não diz de qual parque é."""
        with self.assertRaises(duracoes.Ambigua):
            duracoes.duracao_para_o_parque(self.SEM_CHAVES, "Disney Magic Kingdom")

    def test_com_chave_numerada_pega_a_do_parque_certo(self):
        self.assertEqual(
            duracoes.duracao_para_o_parque(self.COM_CHAVES, "Disney Magic Kingdom"), 9)

    def test_um_resort_so_continua_passando(self):
        self.assertEqual(
            duracoes.duracao_para_o_parque(self.UM_PARQUE, "Disney Animal Kingdom"), 6)

    def test_resorts_citados_olha_so_o_infobox(self):
        """O corpo do artigo cita outros parques o tempo todo; o infobox, não."""
        texto = self.UM_PARQUE + "\n\nVersões parecidas existem na Disneyland Paris."
        self.assertEqual(duracoes.resorts_citados(texto), {"animal kingdom"})


class TestParametrosDoInfobox(unittest.TestCase):
    def test_le_chave_e_valor(self):
        params = duracoes.parametros_do_infobox(
            "{{Infobox attraction\n| name = X\n| duration = 3 minutes\n}}")
        self.assertEqual(params["name"], "X")
        self.assertEqual(params["duration"], "3 minutes")

    def test_template_aninhado_nao_vira_chave_falsa(self):
        """A barra dentro de {{convert|3|min}} não separa parâmetro."""
        params = duracoes.parametros_do_infobox(
            "{{Infobox attraction\n| duration = {{convert|3|min}}\n| name = X\n}}")
        self.assertEqual(params["duration"], "{{convert|3|min}}")
        self.assertEqual(params["name"], "X")

    def test_texto_sem_infobox(self):
        self.assertEqual(duracoes.parametros_do_infobox("Sem infobox aqui."), {})


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


class TestCamposCrus(unittest.TestCase):
    """O `--diagnostico` mostra o infobox sem interpretar.

    O parser recusa artigo de vários parques para não repetir o Pirates, que
    entrou com os 16 min da Disneyland. Mas recusar também joga fora o clone
    idêntico — Seven Dwarfs Mine Train é a mesma atração no Magic Kingdom e em
    Shanghai. Este modo põe o texto na tela para alguém decidir; o que ele NÃO
    pode fazer é interpretar, senão vira o mesmo palpite que se quis evitar.
    """

    def setUp(self):
        self.resposta = {}
        original = duracoes.monitor.get_json
        self.addCleanup(setattr, duracoes.monitor, "get_json", original)

        def falso_get_json(url, *, tentativas=3):
            return self.resposta

        duracoes.monitor.get_json = falso_get_json

    def _pagina(self, wikitexto):
        self.resposta = {"query": {"pages": {"1": {"revisions": [
            {"slots": {"main": {"*": wikitexto}}}]}}}}

    def test_mostra_duracao_por_instalacao_sem_escolher(self):
        """Onde o parser levantaria Ambigua, o diagnóstico entrega os dois lados."""
        self._pagina("{{Infobox\n| park1 = Disneyland\n| duration1 = 16 minutes\n"
                     "| park2 = Magic Kingdom\n| duration2 = 9 minutes\n}}")
        campos = duracoes.campos_crus("Pirates", "Disney Magic Kingdom")
        self.assertEqual(campos["duracoes"],
                         {"duration1": "16 minutes", "duration2": "9 minutes"})
        self.assertEqual(campos["parques_do_infobox"],
                         {"park1": "Disneyland", "park2": "Magic Kingdom"})

    def test_duracao_solta_de_artigo_multiparque_aparece(self):
        """O clone idêntico: uma duração só, dois parques. É o caso a resgatar."""
        self._pagina("{{Infobox\n| park = Magic Kingdom and Shanghai Disneyland\n"
                     "| duration = 3 minutes\n}}")
        campos = duracoes.campos_crus("Seven Dwarfs", "Disney Magic Kingdom")
        self.assertEqual(campos["duracoes"], {"duration": "3 minutes"})
        self.assertIn("magic kingdom", campos["resorts_citados"])
        self.assertIn("shanghai disneyland", campos["resorts_citados"])

    def test_artigo_sem_duracao_nao_inventa_campo(self):
        self._pagina("{{Infobox\n| park = Epcot\n| opened = 2021\n}}")
        self.assertEqual(duracoes.campos_crus("Qualquer", "Epcot")["duracoes"], {})

    def test_pagina_ausente_devolve_vazio(self):
        self.resposta = {"query": {"pages": {"-1": {"missing": ""}}}}
        self.assertEqual(duracoes.campos_crus("Inexistente", "Epcot"), {})


class TestCampoVazioNaoVazaParaOSeguinte(unittest.TestCase):
    """`| duration =` vazio engolia o campo de baixo.

    Visto de verdade no Space Mountain, no Doctor Doom's Fearfall e no Monsters
    Unchained: os três devolviam a linha do `restriction_in`. Nenhum virou
    número errado por sorte — nenhum trazia "minutes" — mas a próxima chave a
    cair ali pode trazer.
    """

    def test_duration_vazio_devolve_None(self):
        texto = ("{{Infobox\n| duration = \n"
                 "| restriction_in      = 52\n| opened = 1999\n}}")
        self.assertIsNone(duracoes.campo_duration(texto))

    def test_duration_vazio_com_comentario_embaixo(self):
        texto = ("{{Infobox\n| duration =\n"
                 "| restriction_ft = <!--Só números-->\n}}")
        self.assertIsNone(duracoes.campo_duration(texto))

    def test_duration_preenchido_continua_lendo(self):
        texto = "{{Infobox\n| duration = 3 minutes\n| opened = 1999\n}}"
        self.assertEqual(duracoes.campo_duration(texto), "3 minutes")

    def test_valor_de_varias_linhas_continua_inteiro(self):
        """O Pirates traz os quatro parques num campo só, separados por <br />."""
        texto = ("{{Infobox\n| duration = '''Disneyland'''<br />15:30 minutes\n"
                 "<br />'''Magic Kingdom'''<br />8:30 minutes\n| opened = 1967\n}}")
        self.assertIn("Magic Kingdom", duracoes.campo_duration(texto))


class TestNumeroPelado(unittest.TestCase):
    """O MEN IN BLACK traz `duration = 5.00` — sem unidade nenhuma."""

    def test_numero_sozinho_e_minuto(self):
        self.assertEqual(duracoes.minutos_do_texto("5.00"), 5)
        self.assertEqual(duracoes.minutos_do_texto(" 3 "), 3)

    def test_numero_no_meio_de_frase_nao_conta(self):
        """Só o campo INTEIRO. Solto no meio pode ser altura, ano ou capacidade."""
        self.assertIsNone(duracoes.minutos_do_texto("52 inches"))
        self.assertIsNone(duracoes.minutos_do_texto("opened in 1999"))


class TestPaginaFixada(unittest.TestCase):
    """Busca por nome caía no artigo do filme Tron e no Space Mountain genérico."""

    def setUp(self):
        original = duracoes.monitor.get_json
        self.addCleanup(setattr, duracoes.monitor, "get_json", original)

        def nunca_chamado(url, *, tentativas=3):
            raise AssertionError(f"não devia consultar a API: {url}")

        duracoes.monitor.get_json = nunca_chamado

    def test_titulo_fixado_dispensa_a_busca(self):
        self.assertEqual(
            duracoes.buscar_pagina("Space Mountain", "Disney Magic Kingdom"),
            "Space Mountain (Magic Kingdom)")
        self.assertEqual(
            duracoes.buscar_pagina("TRON Lightcycle / Run", "Disney Magic Kingdom"),
            "Tron Lightcycle Power Run")

    def test_toda_pagina_fixada_aponta_para_atracao_da_watclist(self):
        """Nome errado aqui vira busca normal em silêncio, sem ninguém notar."""
        import monitor as m
        watchlist = m.load_config()["parks"]
        for (parque, atracao) in duracoes.PAGINAS_WIKI:
            with self.subTest(atracao=atracao):
                self.assertIn(parque, watchlist)
                self.assertIn(atracao, watchlist[parque]["attractions"])


class TestDuracaoPorPista(unittest.TestCase):
    """`duration1`/`duration2` nem sempre são parques — no Space Mountain são pistas.

    O artigo do Magic Kingdom traz `duration1 = 2:30` e `duration2 = 2:30`, as
    pistas Alpha e Omega, sem `park1`/`park2` nenhum. O caminho de artigo
    multiparque não roda (não há instalação numerada) e a chave `duration`
    pelada não existe — a atração ficava sem duração tendo o número na tela.
    """

    def _duracao(self, corpo, parque="Disney Magic Kingdom"):
        return duracoes.duracao_para_o_parque("{{Infobox\n" + corpo + "\n}}", parque)

    def test_pistas_que_concordam_entregam_a_duracao(self):
        self.assertEqual(
            self._duracao("| park = Magic Kingdom\n"
                          "| duration1 = 2:30\n| duration2 = 2:30"), 3)

    def test_pistas_que_divergem_ficam_sem_duracao(self):
        """Qual das duas o visitante pega? Escolher seria estimativa."""
        self.assertIsNone(
            self._duracao("| park = Magic Kingdom\n"
                          "| duration1 = 2:30\n| duration2 = 8:00"))

    def test_duracao_pelada_continua_tendo_prioridade(self):
        self.assertEqual(
            self._duracao("| park = Magic Kingdom\n"
                          "| duration = 4 minutes\n| duration1 = 9:00"), 4)

    def test_nao_vale_para_artigo_de_varios_parques(self):
        """Ali `duration1` É de parque, e quem decide é o casamento por park1."""
        with self.assertRaises(duracoes.Ambigua):
            self._duracao("| park1 = Disneyland\n| duration1 = 16 minutes\n"
                          "| park2 = Tokyo Disneyland\n| duration2 = 9 minutes")


class TestWikidata(unittest.TestCase):
    """Segunda fonte: o P2047. Item por INSTALAÇÃO, não por atração.

    É isso que o infobox não dá: a Haunted Mansion do Magic Kingdom e a da
    Disneyland são Q distintos, então a duração já vem sem a ambiguidade que
    barrou sete atrações. O que a sonda NÃO pode fazer é converter — imprime o
    valor cru com a unidade e quem lê decide, igual ao --diagnostico.
    """

    def setUp(self):
        self.chamadas = []
        self.resposta = {}
        original = duracoes.monitor.get_json
        self.addCleanup(setattr, duracoes.monitor, "get_json", original)

        def falso_get_json(url, *, tentativas=3):
            self.chamadas.append(url)
            return self.resposta

        duracoes.monitor.get_json = falso_get_json

    def _entidade(self, qid, unidade, quantidade="+3", **extra):
        claim = {"mainsnak": {"datavalue": {"value": {
            "amount": quantidade,
            "unit": f"http://www.wikidata.org/entity/{unidade}"}}}}
        return {"entities": {qid: {
            "labels": {"en": {"value": extra.get("rotulo", "Haunted Mansion")}},
            "descriptions": {"en": {"value": extra.get("descricao", "dark ride")}},
            "claims": {"P2047": [claim]}}}}

    def test_a_consulta_vai_para_o_wikidata(self):
        self.resposta = {"search": [{"id": "Q1"}, {"id": "Q2"}]}
        self.assertEqual(duracoes.buscar_itens_wikidata("Haunted Mansion"),
                         ["Q1", "Q2"])
        self.assertIn("wikidata.org", self.chamadas[0])
        self.assertIn("action=wbsearchentities", self.chamadas[0])

    def test_minuto_e_segundo_saem_rotulados(self):
        self.resposta = self._entidade("Q1", "Q7727")
        self.assertIn("+3 min", duracoes.itens_wikidata(["Q1"])[0]["duracao"])
        self.resposta = self._entidade("Q1", "Q11574", "+150")
        self.assertIn("+150 s", duracoes.itens_wikidata(["Q1"])[0]["duracao"])

    def test_unidade_desconhecida_aparece_crua_em_vez_de_virar_minuto(self):
        """Unidade nova tem que dar na vista, nunca virar conversão silenciosa."""
        duracao = self._entidade("Q1", "Q99999")
        self.resposta = duracao
        self.assertIn("(Q99999)", duracoes.itens_wikidata(["Q1"])[0]["duracao"])

    def test_a_descricao_vem_junto_porque_e_ela_que_diz_o_parque(self):
        self.resposta = self._entidade(
            "Q1", "Q7727", descricao="dark ride at Magic Kingdom")
        self.assertEqual(duracoes.itens_wikidata(["Q1"])[0]["descricao"],
                         "dark ride at Magic Kingdom")

    def test_item_sem_P2047_devolve_None(self):
        self.resposta = {"entities": {"Q1": {"claims": {"P31": []}}}}
        self.assertIsNone(duracoes.itens_wikidata(["Q1"])[0]["duracao"])

    def test_sem_candidatos_nao_consulta_nada(self):
        self.assertEqual(duracoes.itens_wikidata([]), [])
        self.assertEqual(self.chamadas, [])

    def test_item_que_o_wikidata_nao_devolveu_e_ignorado(self):
        self.resposta = {"entities": {}}
        self.assertEqual(duracoes.itens_wikidata(["Q1"]), [])


class TestRelatorioWikidataListaTodos(unittest.TestCase):
    """Candidato sem P2047 tambem entra no relatorio.

    Sem isso, "o Wikidata nao tem" era suposicao: o item da atracao podia estar
    fora do limite da busca, empurrado por filme homonimo — e foi filme
    homonimo que apareceu em todas as quatro respostas da primeira rodada.
    """

    def setUp(self):
        original = duracoes.monitor.get_json
        self.addCleanup(setattr, duracoes.monitor, "get_json", original)

        def falso_get_json(url, *, tentativas=3):
            if "wbsearchentities" in url:
                return {"search": [{"id": "Q1"}, {"id": "Q2"}]}
            return {"entities": {
                "Q1": {"labels": {"en": {"value": "Jungle Cruise"}},
                       "descriptions": {"en": {"value": "2021 film"}},
                       "claims": {"P2047": [{"mainsnak": {"datavalue": {"value": {
                           "amount": "+127",
                           "unit": "http://www.wikidata.org/entity/Q7727"}}}}]}},
                "Q2": {"labels": {"en": {"value": "Jungle Cruise"}},
                       "descriptions": {"en": {"value": "boat ride at Magic Kingdom"}},
                       "claims": {}}}}

        duracoes.monitor.get_json = falso_get_json

    def test_o_sem_duracao_aparece_junto_do_com_duracao(self):
        itens = duracoes.itens_wikidata(duracoes.buscar_itens_wikidata("Jungle Cruise"))
        self.assertEqual(len(itens), 2)
        self.assertEqual(itens[0]["duracao"].split()[0], "+127")
        self.assertIsNone(itens[1]["duracao"])
        # é a descrição que denuncia qual dos dois é o filme
        self.assertIn("film", itens[0]["descricao"])
        self.assertIn("Magic Kingdom", itens[1]["descricao"])
