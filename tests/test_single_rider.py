"""Single rider no /status: visível quando tem dado, nunca alertando.

A regra 10 tirava essas filas de tudo, e o motivo era bom — a API publica 0 min
quando não há dado, e o match parcial casa com a atração real. Mas o roteiro
planeja single rider em Flight of Passage, Guardians, Tron e Tiana, e no IOA/USF
não existe Express: esconder por completo tira informação de quem vai usar a
fila. O critério de "tem dado" é o histórico, não o número de agora.
"""
import datetime as dt
import unittest

from tests.apoio import BaseTeste, Resposta

PARQUE = "Epcot"
SINGLE = "Test Track Presented by Chevrolet Single Rider"
PAYLOAD = {"lands": [{"name": "World Discovery", "rides": [
    {"name": "Test Track Presented by Chevrolet", "wait_time": 60, "is_open": True},
    {"name": SINGLE, "wait_time": 15, "is_open": True},
    {"name": "Frozen Ever After", "wait_time": 45, "is_open": True},
]}]}


class BaseSingle(BaseTeste):
    def setUp(self):
        super().setUp()
        self.requests.roteador = lambda url: Resposta(PAYLOAD)

    def gravar_historico(self, valores, atracao=SINGLE, dias=3):
        agora = self.monitor.utc_now()
        for dia in range(1, dias + 1):
            for i, valor in enumerate(valores):
                ts = agora - dt.timedelta(days=dia, minutes=i * 5)
                self.gravar(PARQUE, atracao, valor, ts)

    def status(self):
        return self.monitor.format_status(PARQUE, PAYLOAD, self.config, self.conn)


class TestQuandoAparece(BaseSingle):
    def test_aparece_quando_o_historico_mostra_fila_de_verdade(self):
        self.gravar_historico([10, 15, 20, 25] * 4)
        texto = self.status()
        self.assertIn("Single rider", texto)
        self.assertIn("Test Track — <b>15 min</b>", texto)

    def test_nao_aparece_quando_so_reportou_zero(self):
        """Entrada morta: 0 min ali é ausência de dado, e ausência não vira 0."""
        self.gravar_historico([0] * 16)
        self.assertNotIn("Single rider", self.status())

    def test_nao_aparece_com_historico_curto_demais(self):
        self.gravar_historico([10, 20], dias=1)  # 2 leituras
        self.assertNotIn("Single rider", self.status())

    def test_sem_historico_nenhum_nao_aparece(self):
        self.assertNotIn("Single rider", self.status())

    def test_sem_conexao_nao_aparece(self):
        """O /status aceita conn=None; sem histórico não dá para julgar a fila."""
        self.assertNotIn("Single rider",
                         self.monitor.format_status(PARQUE, PAYLOAD, self.config))

    def test_zero_de_agora_vale_em_fila_viva(self):
        """Numa fila que a API publica de verdade, 0 min é walk-on de verdade."""
        self.gravar_historico([10, 15, 20, 25] * 4)
        payload = {"lands": [{"name": "L", "rides": [
            {"name": "Test Track Presented by Chevrolet", "wait_time": 60, "is_open": True},
            {"name": SINGLE, "wait_time": 0, "is_open": True},
        ]}]}
        self.assertIn("Test Track — <b>0 min</b>",
                      self.monitor.format_status(PARQUE, payload, self.config, self.conn))

    def test_fila_fechada_ou_obsoleta_nao_aparece(self):
        self.gravar_historico([10, 15, 20, 25] * 4)
        velho = (self.monitor.utc_now() - dt.timedelta(hours=5)).isoformat()
        for ride in ({"name": SINGLE, "wait_time": 15, "is_open": False},
                     {"name": SINGLE, "wait_time": 15, "is_open": True,
                      "last_updated": velho}):
            with self.subTest(ride=ride):
                payload = {"lands": [{"name": "L", "rides": [
                    {"name": "Frozen Ever After", "wait_time": 45, "is_open": True},
                    ride]}]}
                self.assertNotIn(
                    "Single rider",
                    self.monitor.format_status(PARQUE, payload, self.config, self.conn))


class TestNaoContamina(BaseSingle):
    """Regra 10 continua valendo em tudo que decide ir ou não ir."""

    def test_continua_fora_do_alerta(self):
        self.gravar_historico([10, 15, 20, 25] * 4)
        self.assertIsNone(self.monitor.nome_watchlist(
            self.config["parks"][PARQUE], SINGLE))
        self.assertIsNone(self.monitor.get_threshold(
            self.config["parks"][PARQUE], SINGLE))

    def test_continua_fora_dos_rankings(self):
        self.gravar_historico([10, 15, 20, 25] * 4)
        menores = self.monitor.menores_filas(PAYLOAD, self.config, PARQUE, 10,
                                             apenas_watchlist=False)
        maiores = self.monitor.maiores_filas(PAYLOAD, self.config, 10)
        self.assertNotIn(SINGLE, [nome for _w, nome, _t in menores])
        self.assertNotIn(SINGLE, [nome for _w, nome in maiores])

    def test_nome_da_api_nunca_vai_para_a_tela(self):
        """Sai o nome da watchlist, não 'Test Track Presented by Chevrolet...'."""
        self.gravar_historico([10, 15, 20, 25] * 4)
        self.assertNotIn("Presented by Chevrolet Single Rider", self.status())

    def test_bloco_avisa_que_nao_alerta(self):
        self.gravar_historico([10, 15, 20, 25] * 4)
        self.assertIn("nunca alerta", self.status())


class TestCasamentoComAAtracao(BaseTeste):
    def park_cfg(self):
        return self.config["parks"]["Universal Epic Universe"]

    def test_casa_pelo_nome_da_watchlist_dentro_do_nome_da_fila(self):
        self.assertEqual(
            self.monitor.atracao_da_fila_paralela(
                self.park_cfg(), "Mario Kart™: Bowser's Challenge Single Rider"),
            "Mario Kart: Bowser's Challenge")

    def test_atracao_normal_nao_e_fila_paralela(self):
        self.assertIsNone(self.monitor.atracao_da_fila_paralela(
            self.park_cfg(), "Stardust Racers"))

    def test_fila_paralela_fora_da_watchlist_fica_de_fora(self):
        self.assertIsNone(self.monitor.atracao_da_fila_paralela(
            self.park_cfg(), "Atração Que Ninguém Pediu Single Rider"))


if __name__ == "__main__":
    unittest.main()
