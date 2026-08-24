"""O que a tela do site mostra, exercitando o app.js de verdade.

Dois defeitos chegaram ao celular com o CI verde, e os dois eram JavaScript —
território que nenhum teste de Python alcançava:

1. Parque fechado deixava a aba "Melhores agora" completamente em branco:
   cabeçalho com o nome do parque e nada abaixo, indistinguível de falha de
   rede.
2. A vigia mostrava "fila agora 0 min" para atração fechada — a Queue-Times
   publica 0 quando não está funcionando. Com limite absoluto o 0 satisfazia
   qualquer alvo e a barra dizia "no alvo", prometendo um alerta que o
   Telegram nunca manda: `maybe_alertar_fila_baixa` exige `is_open`.

O harness roda o app.js num DOM mínimo (`tests/harness_site.js`). Node não é
dependência do projeto — é o que o runner do CI já traz —, então o teste é
pulado onde ele não existir, nunca falso-verde.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

NODE = shutil.which("node")
HARNESS = os.path.join(os.path.dirname(__file__), "harness_site.js")


def render(caso: dict) -> dict:
    saida = subprocess.run(
        [NODE, HARNESS, json.dumps(caso)],
        capture_output=True, text=True, timeout=30)
    if saida.returncode != 0:
        raise AssertionError(f"o harness falhou:\n{saida.stderr}")
    return json.loads(saida.stdout)


def vigia(**campos) -> dict:
    base = {"park": "Disney Animal Kingdom", "ride": "Kilimanjaro Safaris",
            "quem": "Jj", "limite_min": None, "limite_pct": 85,
            "alvo_min": None, "tipico_min": None, "fila_agora": None,
            "aberta": True, "criado_em": "2026-08-24T22:00:00+00:00"}
    base.update(campos)
    return base


def painel_vigias(*vigias) -> dict:
    return {"vigias": list(vigias), "max_por_chat": 5,
            "attribution": "Powered by Queue-Times.com"}


@unittest.skipUnless(NODE, "node não disponível — teste de tela pulado")
class TestPertoVazio(unittest.TestCase):
    def resposta(self, items, abertas):
        return {"aba": "perto", "gps": True, "respostas": {
            "/perto": {"park": "Disney Animal Kingdom", "items": items,
                       "abertas": abertas, "source": "fila-disney-vps",
                       "attribution": "Powered by Queue-Times.com"}}}

    def test_parque_fechado_explica_em_vez_de_apagar_a_tela(self):
        tela = render(self.resposta([], 0))
        self.assertIn("Parque fechado agora", tela["perto"])
        self.assertIn("Disney Animal Kingdom", tela["subtitulo"])

    def test_parque_aberto_sem_item_elegivel_diz_outra_coisa(self):
        """Os dois vazios têm causas diferentes; um texto só esconderia isso."""
        tela = render(self.resposta([], 12))
        self.assertIn("velhas ou sem coordenada", tela["perto"])
        self.assertNotIn("Parque fechado", tela["perto"])

    def test_com_itens_a_lista_aparece_normalmente(self):
        item = {"name": "Expedition Everest", "wait": 25, "walk": 6,
                "meters": 430, "total": 31, "coordinate": None,
                "route_source": "estimada", "quality": 72}
        tela = render(self.resposta([item], 12))
        self.assertIn("Expedition Everest", tela["perto"])
        self.assertIn("fila 25 min", tela["perto"])
        self.assertNotIn("Parque fechado", tela["perto"])


@unittest.skipUnless(NODE, "node não disponível — teste de tela pulado")
class TestVigiaFechada(unittest.TestCase):
    def render_vigias(self, *vs):
        return render({"aba": "vigias", "respostas": {"/vigias": painel_vigias(*vs)}})

    def test_atracao_fechada_nao_vira_fila_de_zero_minuto(self):
        tela = self.render_vigias(vigia(fila_agora=0, aberta=False))
        self.assertIn("fechada agora", tela["vigias"])
        self.assertNotIn("0 min", tela["vigias"])

    def test_fechada_com_limite_absoluto_nao_anuncia_no_alvo(self):
        """O caso que mentia: 0 ≤ qualquer limite, e a tela prometia um alerta
        que o monitor jamais dispara porque a atração não está aberta."""
        tela = self.render_vigias(
            vigia(fila_agora=0, aberta=False, limite_min=40,
                  limite_pct=None, alvo_min=40))
        self.assertNotIn("no alvo", tela["vigias"])
        self.assertIn("fechada agora", tela["vigias"])

    def test_aberta_no_alvo_continua_anunciando(self):
        tela = self.render_vigias(
            vigia(fila_agora=15, aberta=True, limite_min=40,
                  limite_pct=None, alvo_min=40))
        self.assertIn("fila agora 15 min", tela["vigias"])
        self.assertIn("no alvo", tela["vigias"])

    def test_sem_leitura_a_fila_fica_em_travessao(self):
        """Regra 15 na tela: ausência de dado não é 0 e não é 'fechada'."""
        tela = self.render_vigias(vigia(fila_agora=None, aberta=None))
        self.assertIn("fila agora —", tela["vigias"])

    def test_percentual_sem_historico_nao_inventa_alvo(self):
        tela = self.render_vigias(vigia(fila_agora=30, aberta=True))
        self.assertIn("aguardando histórico", tela["vigias"])



def parque_payload(**campos) -> dict:
    base = {"park": "Epcot", "horario": {"abre": 9, "fecha": 21},
            "lotacao": {"nivel": "cheia", "fechadas": 2},
            "items": [], "outras": [], "shows": [],
            "attribution": "Powered by Queue-Times.com"}
    base.update(campos)
    return base


@unittest.skipUnless(NODE, "node não disponível — teste de tela pulado")
class TestAbaParques(unittest.TestCase):
    """A aba que mostra o parque inteiro e roda os comandos do Telegram."""

    def tela(self, *, clicar=None, comando=None, **payload):
        respostas = {
            "/comandos": {"comandos": [{"cmd": "menores", "rotulo": "Menores filas"},
                                       {"cmd": "status", "rotulo": "Status"}],
                          "parques": ["Epcot", "Disney Animal Kingdom"]},
            "/parque": parque_payload(**payload),
        }
        if comando is not None:
            respostas["/comando"] = {"comando": "menores", "parque": "Epcot",
                                     "texto": comando,
                                     "attribution": "Powered by Queue-Times.com"}
        caso = {"aba": "parques", "respostas": respostas}
        if clicar:
            caso["clicar"] = clicar
        return render(caso)

    def test_lista_os_parques_e_o_estado_do_escolhido(self):
        tela = self.tela()
        self.assertIn("Epcot", tela["parques"])
        self.assertIn("Disney Animal Kingdom", tela["parques"])
        self.assertIn("opera ~09h–21h pelo histórico", tela["parques"])
        self.assertIn("lotação cheia", tela["parques"])

    def test_show_aparece_sem_numero_de_fila(self):
        """O pedido que originou a aba. Show tem wait 0 permanente: mostrar
        "0 min" seria dizer que não há espera onde não há medição (regra 15),
        e mostrá-lo no ranking por fila o poria em primeiro sempre."""
        tela = self.tela(shows=[{"ride": "Awesome Planet", "aberta": True},
                                {"ride": "Impressions de France", "aberta": False}])
        self.assertIn("Awesome Planet", tela["parques"])
        self.assertIn("em cartaz", tela["parques"])
        self.assertIn("Impressions de France", tela["parques"])
        self.assertIn("fechada", tela["parques"])
        self.assertNotIn("0 min", tela["parques"])

    def test_separa_watchlist_de_outras_atracoes(self):
        tela = self.tela(
            items=[{"ride": "Test Track", "wait": 40, "threshold": 30, "aberta": True,
                    "obsoleta": False, "duracao_min": 4, "pre_min": None}],
            outras=[{"ride": "Spaceship Earth", "wait": 15, "aberta": True,
                     "obsoleta": False}])
        self.assertIn("Na watchlist", tela["parques"])
        self.assertIn("Test Track", tela["parques"])
        self.assertIn("Outras atrações", tela["parques"])
        self.assertIn("Spaceship Earth", tela["parques"])

    def test_secao_vazia_nao_vira_titulo_solto(self):
        tela = self.tela()
        for titulo in ("Na watchlist", "Outras atrações", "Shows e sem fila"):
            self.assertNotIn(titulo, tela["parques"])

    def test_botao_roda_o_comando_e_mostra_o_texto_do_telegram(self):
        tela = self.tela(clicar="Menores filas",
                         comando="<b>Menores filas — Epcot</b>\n<code>Test Track</code> 40 min")
        self.assertIn("Menores filas — Epcot", tela["parques"])
        self.assertIn("Test Track", tela["parques"])


@unittest.skipUnless(NODE, "node não disponível — teste de tela pulado")
class TestHtmlDoTelegram(unittest.TestCase):
    """O site mostra o MESMO texto que o Telegram, e o converte sem innerHTML.

    O conteúdo vem do nosso formatador e já passa pelo `notifier.esc` (regra 8),
    mas a tela não confia nisso: constrói a árvore com uma lista fechada de
    tags, então um `&` que escapasse do esc vira texto, nunca execução.
    """

    def converter(self, html: str) -> dict:
        return render({"telegram": html})

    def test_preserva_as_tags_que_o_telegram_usa(self):
        r = self.converter("<b>Epcot</b> — <code>40 min</code> <i>agora</i>")
        self.assertIn("<b>Epcot</b>", r["estrutura"])
        self.assertIn("<code>40 min</code>", r["estrutura"])
        self.assertIn("<i>agora</i>", r["estrutura"])

    def test_tag_desconhecida_perde_a_marcacao_e_mantem_o_texto(self):
        """Some o destaque, nunca a informação."""
        r = self.converter("<script>alert(1)</script>fim")
        self.assertNotIn("<script", r["estrutura"])
        self.assertIn("fim", r["estrutura"])
        self.assertIn("alert(1)", r["estrutura"], "o texto continua legível")

    def test_link_javascript_perde_o_href(self):
        r = self.converter('<a href="javascript:alert(1)">clique</a>')
        self.assertNotIn("javascript", r["estrutura"])
        self.assertIn("clique", r["estrutura"])

    def test_link_http_sobrevive(self):
        """O /perto manda rota do Google Maps; ela tem que continuar clicável."""
        r = self.converter('<a href="https://maps.google.com/dir">rota</a>')
        self.assertIn('href="https://maps.google.com/dir"', r["estrutura"])

    def test_entidades_viram_o_caractere(self):
        """"Mickey & Minnie's" chega escapado do esc (regra 8) e tem que
        aparecer com o & na tela, não como &amp;."""
        r = self.converter("Mickey &amp; Minnie&#39;s &lt;x&gt;")
        self.assertIn("Mickey & Minnie's", r["texto"])
        self.assertNotIn("&amp;", r["texto"])

    def test_texto_sem_tag_nenhuma_passa_inteiro(self):
        r = self.converter("Nenhuma atração aberta agora.")
        self.assertIn("Nenhuma atração aberta agora.", r["texto"])

@unittest.skipUnless(NODE, "node não disponível — teste de tela pulado")
class TestCorDaFila(unittest.TestCase):
    """A cor diz "pode ir agora"; fechada não pode ficar verde.

    Visto no celular em 24/08: o Animal Kingdom fechado listava
    "Avatar Flight of Passage — fechada" em VERDE. A atração fechada publica
    wait 0, 0 passa em qualquer threshold, e a função de cor não sabia se a
    atração estava aberta.
    """

    def linha(self, **item):
        base = {"ride": "Avatar Flight of Passage", "wait": 0, "threshold": 60,
                "aberta": False, "obsoleta": False, "duracao_min": None,
                "pre_min": None}
        base.update(item)
        return render({"aba": "parques", "respostas": {
            "/comandos": {"comandos": [], "parques": ["Disney Animal Kingdom"]},
            "/parque": {"park": "Disney Animal Kingdom", "horario": None,
                        "lotacao": None, "items": [base], "outras": [], "shows": [],
                        "attribution": "Powered by Queue-Times.com"}}})

    def test_fechada_nao_recebe_cor_de_fila_boa(self):
        tela = self.linha()
        self.assertIn("fechada", tela["parques"])
        self.assertNotIn("fila-ok", tela["classes"])

    def test_aberta_dentro_do_limite_fica_verde(self):
        tela = self.linha(aberta=True, wait=20)
        self.assertIn("fila-ok", tela["classes"])

    def test_aberta_acima_do_limite_fica_vermelha(self):
        tela = self.linha(aberta=True, wait=90)
        self.assertIn("fila-alta", tela["classes"])


class TestEstiloCompartilhado(unittest.TestCase):
    """Classe de layout não pode ficar presa ao lugar onde nasceu.

    A `.linha` foi escrita como `.filas-live .linha`, para a aba Roteiro. A aba
    Parques reusou a classe e as duas colunas saíram coladas na tela —
    "Avatar Flight of Passagefechada". Não é bug de lógica e o harness não
    avalia CSS, então o que dá para afirmar aqui é o seletor.
    """

    def css(self) -> str:
        with open("site/styles.css", encoding="utf-8") as f:
            return f.read()

    def test_classes_de_layout_valem_em_qualquer_aba(self):
        css = self.css()
        for classe in (".linha", ".meta"):
            with self.subTest(classe=classe):
                # A REGRA BASE, não uma variante: `.linha span:last-child`
                # existir não garante que `.linha` sozinha tenha o flex.
                # `assertTrue` e não `assertRegex` porque este despeja o
                # styles.css inteiro na falha e esconde a mensagem.
                self.assertTrue(
                    re.search(rf"(?m)^\{classe}\s*\{{", css),
                    f"{classe} só existe aninhada — quem reusar a classe fora do "
                    f"bloco original perde o estilo em silêncio")


if __name__ == "__main__":
    unittest.main()
