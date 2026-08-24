"""Vigia de fila por chat: "/vigiar everest 40" e o modo percentual.

A metade que faltava do monitoramento por escolha do usuário: a vigia de
reabertura já era por chat, mas o alerta de queda de fila era global (threshold
da watchlist, só no dia do parque, só no chat principal). Agora cada pessoa
escolhe até 5 atrações e o limite — absoluto ou relativo ao típico do horário.
"""
import unittest

from tests.apoio import BaseTeste

PARQUE = "Disney Animal Kingdom"
RIDE = "Expedition Everest"


def payload(nome, fila, aberta=True):
    return {"lands": [{"name": "L", "rides": [
        {"id": 1, "name": nome, "is_open": aberta, "wait_time": fila}]}]}


class TestComando(BaseTeste):
    def _vigiar(self, texto):
        return self.monitor.handle_command(
            texto, self.conn, self.config, {PARQUE: 8}, chat_id="4242")

    def test_numero_vira_vigia_de_fila(self):
        r = self._vigiar("/vigiar everest 40")
        self.assertIn("40 min ou menos", r)

    def test_percentual_vira_vigia_relativa(self):
        r = self._vigiar("/vigiar everest 50%")
        self.assertIn("50% do típico", r)

    def test_sem_numero_continua_sendo_reabertura(self):
        r = self._vigiar("/vigiar everest")
        self.assertIn("fechada para aberta", r)

    def test_limite_fora_da_faixa_e_recusado(self):
        self.assertIn("fora da faixa", self._vigiar("/vigiar everest 0"))
        self.assertIn("fora da faixa", self._vigiar("/vigiar everest 999"))
        self.assertIn("fora da faixa", self._vigiar("/vigiar everest 100%"))

    def test_maximo_cinco_por_chat(self):
        for atracao in ("everest", "kilimanjaro", "kali", "na'vi", "flight of passage"):
            self.assertIn("Vou vigiar", self._vigiar(f"/vigiar {atracao} 30"))
        r = self._vigiar("/vigiar slinky 30")
        self.assertIn("o limite é 5", r)

    def test_reeditar_atracao_ja_vigiada_nao_conta_como_nova(self):
        for atracao in ("everest", "kilimanjaro", "kali", "na'vi", "flight of passage"):
            self._vigiar(f"/vigiar {atracao} 30")
        self.assertIn("Vou vigiar", self._vigiar("/vigiar everest 20"))

    def test_cancelar_remove_e_libera_vaga(self):
        self._vigiar("/vigiar everest 40")
        self.assertIn("Vigilância removida", self._vigiar("/vigiar cancelar everest"))
        self.assertIn("Nenhuma atração sendo vigiada", self._vigiar("/vigiar"))

    def test_lista_mostra_o_limite_de_cada_vigia(self):
        self._vigiar("/vigiar everest 40")
        self._vigiar("/vigiar kilimanjaro 50%")
        lista = self._vigiar("/vigiar")
        self.assertIn("≤ 40 min", lista)
        self.assertIn("≤ 50% do típico", lista)
        self.assertIn("2 de 5", lista)


class TestDisparo(BaseTeste):
    def _armar(self, limite, percentual=False):
        self.monitor.vigiar_fila(self.conn, PARQUE, RIDE, limite, percentual, "4242")

    def _ciclo(self, fila, nome=None, aberta=True, estado="operando"):
        ride = {"id": 1, "name": nome or RIDE, "is_open": aberta,
                "wait_time": fila,
                "last_updated": self.monitor.utc_now().isoformat()}
        self.monitor.maybe_alertar_fila_baixa(self.conn, self.config, PARQUE,
                                              ride, estado)

    def _avisos(self):
        return [p["text"] for p in self.requests.posts
                if isinstance(p, dict) and "vigia atendida" in str(p.get("text", ""))]

    def test_dispara_no_limite_e_se_apaga(self):
        self._armar(40)
        self._ciclo(35)
        self._ciclo(30)
        self.assertEqual(len(self._avisos()), 1, "uma vez, e só uma")
        self.assertIn("35 min", self._avisos()[0])

    def test_acima_do_limite_nao_dispara(self):
        self._armar(40)
        self._ciclo(45)
        self.assertEqual(self._avisos(), [])

    def test_nome_decorado_da_api_dispara(self):
        """A vigia guarda o canônico; a API manda decorado."""
        self._armar(40)
        self._ciclo(30, nome="Expedition Everest - Legend of the Forbidden Mountain")
        self.assertEqual(len(self._avisos()), 1)

    def test_fila_none_nao_dispara(self):
        """Regra 15: ausência de dado nunca vira 0 min."""
        self._armar(40)
        self._ciclo(None)
        self.assertEqual(self._avisos(), [])

    def test_parque_fora_de_operacao_nao_dispara(self):
        """De madrugada a fila é 0 de placeholder, não oportunidade."""
        self._armar(40)
        self._ciclo(0, estado="fechado")
        self.assertEqual(self._avisos(), [])

    def test_percentual_sem_historico_nao_dispara(self):
        """Regra 12: sem perfil não há 'típico' — melhor calar que chutar."""
        self._armar(50, percentual=True)
        self._ciclo(5)
        self.assertEqual(self._avisos(), [])

    def test_percentual_dispara_com_historico(self):
        agora = self.monitor.now_park(self.config)
        ts = agora.astimezone(self.monitor.timezone.utc).replace(tzinfo=None)
        for i in range(15):  # perfil pede >= 12 amostras da mesma hora/dia
            self.gravar(PARQUE, RIDE, 60, ts.replace(minute=i % 60))
        self._armar(50, percentual=True)
        self._ciclo(25)   # 25 <= 50% de 60
        self.assertEqual(len(self._avisos()), 1)
        self.assertIn("50%", self._avisos()[0])

    def test_percentual_acima_do_corte_nao_dispara(self):
        agora = self.monitor.now_park(self.config)
        ts = agora.astimezone(self.monitor.timezone.utc).replace(tzinfo=None)
        for i in range(15):
            self.gravar(PARQUE, RIDE, 60, ts.replace(minute=i % 60))
        self._armar(50, percentual=True)
        self._ciclo(45)   # 45 > 30
        self.assertEqual(self._avisos(), [])

    def test_falha_de_envio_mantem_a_vigia(self):
        self._armar(40)
        self.requests.roteador_post = lambda url, corpo: (_ for _ in ()).throw(
            self.requests.RequestException("Telegram fora"))
        self._ciclo(30)
        restante = self.conn.execute(
            "SELECT COUNT(*) FROM fila_watches").fetchone()[0]
        self.assertEqual(restante, 1, "sem envio confirmado, a vigia fica")
