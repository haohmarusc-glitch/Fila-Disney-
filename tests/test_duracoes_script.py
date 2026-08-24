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
