"""Aviso de parque que parou de coletar.

O ponto cego que motivou isto: tudo que media a coleta olhava o conjunto.
O `healthcheck.py` do container e o `/health` usam `MAX(ts)` de TODOS os
parques juntos, e o heartbeat do Uptime Kuma só se cala quando um FETCH falha.
Com seis parques gravando e um morto, os três ficavam verdes.
"""
from datetime import timedelta

from tests import apoio


class BaseColeta(apoio.BaseTeste):
    PARQUES = {"Epcot": 5, "Disney Magic Kingdom": 6, "Universal Epic Universe": 334}

    def cenario(self, atrasos=None):
        """Uma leitura por parque, com a idade pedida em minutos (padrão: 1).

        Apaga antes de gravar: acrescentar uma linha velha a um parque que já
        tem linha nova não o deixa "parado" — o `MAX(ts)` continua fresco. Foi
        assim que a primeira versão destes testes mediu a coisa errada.
        """
        self.conn.execute("DELETE FROM wait_times")
        for park in self.PARQUES:
            minutos = (atrasos or {}).get(park, 1)
            self.gravar(park, "Atração", 30,
                        self.monitor.utc_now() - timedelta(minutes=minutos))

    def checar(self):
        self.monitor.maybe_alertar_coleta_parada(self.conn, self.config, self.PARQUES)

    def marcados(self):
        return {l[0] for l in self.conn.execute("SELECT park FROM coleta_alertas")}


class TestAviso(BaseColeta):
    def test_parque_parado_rende_um_aviso_com_o_nome_e_o_atraso(self):
        self.cenario({"Epcot": 47})
        self.checar()
        (msg,) = self.enviadas()
        self.assertIn("Coleta parada", msg)
        self.assertIn("Epcot", msg)
        self.assertIn("47 min", msg)

    def test_nao_repete_no_ciclo_seguinte(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.checar()
        self.assertEqual(len(self.enviadas()), 1, "o aviso repetiu")

    def test_um_ciclo_falho_nao_alerta(self):
        """A Queue-Times cai e volta. Avisar a cada tropeço treina a ignorar."""
        self.cenario({"Epcot": 12})   # 2 ciclos perdidos
        self.checar()
        self.assertEqual(self.enviadas(), [])

    def test_parque_que_nunca_coletou_nao_alerta(self):
        """Banco novo tem sete parques sem leitura: seriam sete avisos no boot."""
        self.checar()
        self.assertEqual(self.enviadas(), [])
        self.assertEqual(self.marcados(), set())

    def test_conta_quantos_quando_e_mais_de_um(self):
        self.cenario({"Epcot": 40, "Disney Magic Kingdom": 40})
        self.checar()
        self.assertTrue(any("2 de 3 parques parados" in m for m in self.enviadas()))

    def test_um_parado_nao_menciona_os_outros(self):
        self.cenario({"Epcot": 40})
        self.checar()
        self.assertNotIn("parques parados", self.enviadas()[0])


class TestPayloadVazio(BaseColeta):
    def test_pega_o_parque_que_responde_200_sem_atracao(self):
        """O caso que o heartbeat NÃO vê: o fetch deu certo, então o ciclo se
        diz completo e o Kuma segue verde — mas não houve linha para gravar."""
        self.cenario({"Universal Epic Universe": 90})
        parados = dict(self.monitor.parques_parados(self.conn, self.PARQUES))
        self.assertIn("Universal Epic Universe", parados)
        self.assertNotIn("Epcot", parados)


class TestVolta(BaseColeta):
    def test_avisa_quando_volta_e_esquece_o_parque(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.cenario()
        self.checar()
        self.assertIn("Coleta retomada", self.enviadas()[-1])
        self.assertEqual(self.marcados(), set())

    def test_queda_nova_depois_da_volta_alerta_de_novo(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.cenario()
        self.checar()
        self.cenario({"Epcot": 47})
        self.checar()
        self.assertEqual(sum("Coleta parada" in m for m in self.enviadas()), 2)

    def test_parque_tirado_da_watchlist_some_sem_dizer_que_voltou(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.monitor.maybe_alertar_coleta_parada(
            self.conn, self.config, {"Disney Magic Kingdom": 6})
        self.assertNotIn("retomada", " ".join(self.enviadas()))
        self.assertEqual(self.marcados(), set())


class TestQuietHours(BaseColeta):
    def setUp(self):
        super().setUp()
        self.config.setdefault("alert", {})["quiet_hours"] = {"start": "00:00", "end": "23:59"}

    def test_nao_acorda_ninguem_e_nem_marca_como_avisado(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.assertEqual(self.enviadas(), [])
        self.assertEqual(self.marcados(), set(), "marcar aqui engoliria o aviso")

    def test_o_aviso_sai_no_primeiro_ciclo_depois(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.config["alert"]["quiet_hours"] = None
        self.checar()
        self.assertIn("Coleta parada", self.enviadas()[0])

    def test_volta_no_silencio_nao_anuncia_recuperacao_que_ninguem_viu(self):
        self.cenario({"Epcot": 47})
        self.checar()
        self.cenario()
        self.config["alert"]["quiet_hours"] = None
        self.checar()
        self.assertEqual(self.enviadas(), [])


class TestTelegramFora(BaseColeta):
    def test_envio_falho_nao_marca_e_o_aviso_volta(self):
        """Marcar sem ter enviado deixaria o parque parado e silencioso."""
        self.cenario({"Epcot": 47})
        self.requests.roteador_post = lambda url, payload: apoio.Resposta(
            None, status=500, texto="erro")
        self.checar()
        self.assertEqual(self.marcados(), set())
        self.requests.roteador_post = lambda url, payload: apoio.Resposta({"ok": True})
        self.checar()
        self.assertIn("Coleta parada", self.enviadas()[-1])


class TestHealth(BaseColeta):
    def test_um_parque_morto_pinta_o_painel_de_vermelho(self):
        """Seis frescos levantam o MAX(ts) global: sem olhar parque a parque,
        o comando que se abre para conferir a suspeita responde 🟢."""
        self.cenario({"Epcot": 90})
        texto = self.monitor.format_health(self.conn, self.config, self.PARQUES)
        self.assertIn("🔴", texto.splitlines()[0])
        self.assertIn("Epcot", texto)
        self.assertIn("sem gravar há 90 min", texto)

    def test_tudo_fresco_continua_verde_e_sem_linha_extra(self):
        self.cenario()
        texto = self.monitor.format_health(self.conn, self.config, self.PARQUES)
        self.assertIn("🟢", texto.splitlines()[0])
        self.assertNotIn("sem gravar há", texto)
