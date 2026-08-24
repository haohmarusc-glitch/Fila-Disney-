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


if __name__ == "__main__":
    unittest.main()
