"""Janela noturna, healthcheck da API e espaço em disco.

A pesquisa de reclamações de visitantes aponta a queda de 30-50% nas filas
durante fogos, parada e pós-jantar como a dica mais repetida. A hora em que
isso acontece muda por parque e por temporada — então é medida no histórico,
não cravada no código.
"""
import datetime as dt
import unittest
from unittest.mock import patch

import healthcheck_api
from tests.apoio import BaseTeste

PARQUE = "Disney Magic Kingdom"
# Seis: o índice exige um mínimo de atrações por hora, senão a "média do
# parque" seria a média de duas coisas.
ATRACOES = ["Space Mountain", "Haunted Mansion", "Jungle Cruise",
            "Peter Pan's Flight", "Big Thunder Mountain Railroad",
            "Pirates of the Caribbean"]


class BasePerfil(BaseTeste):
    def gravar_perfil(self, medias_por_hora_local, dias=3, por_hora=12):
        """Grava um perfil sintético: {hora local: fila média}."""
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        linhas = []
        for dia in range(1, dias + 1):
            data = hoje - dt.timedelta(days=dia)
            for hora_local, media in medias_por_hora_local.items():
                # a hora local vira UTC pelo offset daquele dia
                local = dt.datetime.combine(data, dt.time(hora_local), tzinfo=fuso)
                utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(por_hora):
                    ts = (utc + dt.timedelta(minutes=i)).isoformat()
                    for atracao in ATRACOES:
                        linhas.append((ts, PARQUE, "L", atracao, media, 1))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()

    # Pico às 14h, queda forte a partir das 20h — o formato que a pesquisa descreve.
    PERFIL_TIPICO = {9: 20, 10: 40, 12: 55, 14: 60, 16: 55, 18: 50, 20: 35, 21: 25}


class TestDeteccaoDaJanela(BasePerfil):
    def test_acha_a_primeira_hora_depois_do_pico_com_queda(self):
        self.gravar_perfil(self.PERFIL_TIPICO)
        hora, queda, pico = self.monitor.janela_noturna(self.conn, self.config, PARQUE)
        self.assertEqual(hora, 20)
        self.assertEqual(pico, 14)
        # A queda é contra a hora TÍPICA (mediana), não contra o pico: 35 min
        # numa mediana de ~45 dá ~22%, e não os 42% que o pico sugeriria.
        self.assertAlmostEqual(queda, 22.2, places=0)

    def test_manha_nao_conta_mesmo_com_fila_baixa(self):
        """9h tem fila menor que 20h, mas ali a decisão é rope drop, não 'vai agora'."""
        self.gravar_perfil(self.PERFIL_TIPICO)
        hora, _queda, _pico = self.monitor.janela_noturna(self.conn, self.config, PARQUE)
        self.assertGreater(hora, 14, "a janela é depois do pico")

    def test_parque_sem_queda_nao_inventa_janela(self):
        self.gravar_perfil({9: 40, 12: 45, 14: 46, 16: 45, 18: 44, 20: 43})
        self.assertIsNone(self.monitor.janela_noturna(self.conn, self.config, PARQUE))

    def test_hora_com_poucas_amostras_e_ignorada(self):
        self.gravar_perfil(self.PERFIL_TIPICO, dias=1, por_hora=1)  # 1 leitura/hora
        self.assertIsNone(self.monitor.janela_noturna(self.conn, self.config, PARQUE),
                          "uma leitura por hora não descreve o parque")

    def test_sem_historico_nenhum(self):
        self.assertIsNone(self.monitor.janela_noturna(self.conn, self.config, PARQUE))

    def test_queda_minima_e_configuravel(self):
        self.gravar_perfil({9: 20, 12: 50, 14: 60, 16: 58, 18: 55, 20: 44})  # queda de ~16%
        self.assertIsNone(self.monitor.janela_noturna(self.conn, self.config, PARQUE))
        config = dict(self.config, evening_alert={"min_drop_percent": 10})
        self.assertIsNotNone(self.monitor.janela_noturna(self.conn, config, PARQUE))

    def test_atracao_que_so_reporta_zero_nao_entra_no_indice(self):
        """Cinco atrações do Hollywood Studios reportam fila 0 as 24 horas."""
        self.gravar_perfil(self.PERFIL_TIPICO)
        sem_zero = self.monitor.janela_noturna(self.conn, self.config, PARQUE)
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        linhas = []
        for dia in range(1, 4):
            for hora_local in self.PERFIL_TIPICO:
                local = dt.datetime.combine(hoje - dt.timedelta(days=dia),
                                            dt.time(hora_local), tzinfo=fuso)
                utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(12):
                    ts = (utc + dt.timedelta(minutes=i)).isoformat()
                    linhas.append((ts, PARQUE, "L", "Walt Disney Presents", 0, 1))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()
        self.assertEqual(self.monitor.janela_noturna(self.conn, self.config, PARQUE),
                         sem_zero, "atração sempre em 0 não diz nada sobre lotação")


class TestAvisoDaJanela(BasePerfil):
    def setUp(self):
        super().setUp()
        self.gravar_perfil(self.PERFIL_TIPICO)
        self.config["park_days"] = {self.hoje(): [PARQUE]}
        self.payloads = {PARQUE: {"lands": [{"name": "L", "rides": [
            {"id": 1, "name": "Space Mountain", "is_open": True, "wait_time": 15},
            {"id": 2, "name": "Haunted Mansion", "is_open": True, "wait_time": 20},
        ]}]}}

    def hoje(self):
        return self.monitor.now_park(self.config).date().isoformat()

    def em(self, hora):
        fuso = self.monitor.fuso_do_parque(self.config)
        agora = dt.datetime.now(fuso).replace(hour=hora, minute=5)
        self.monitor.now_park = lambda _cfg: agora

    def test_avisa_na_hora_da_janela(self):
        self.em(20)
        self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6},
                                              self.payloads)
        texto = self.enviadas()[0]
        self.assertIn("Janela de fila curta", texto)
        self.assertIn("Space Mountain", texto)
        self.assertIn("14h", texto, "diz qual era o pico, para a queda fazer sentido")

    def test_nao_avisa_fora_da_hora(self):
        self.em(16)
        self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6},
                                              self.payloads)
        self.assertEqual(self.enviadas(), [])

    def test_avisa_uma_vez_por_dia(self):
        self.em(20)
        for _ in range(6):  # seis ciclos dentro da mesma hora
            self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6},
                                                  self.payloads)
        self.assertEqual(len(self.enviadas()), 1)

    def test_nao_avisa_em_dia_sem_parque(self):
        self.config["park_days"] = {}
        self.em(20)
        self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6},
                                              self.payloads)
        self.assertEqual(self.enviadas(), [])

    def test_parque_que_falhou_no_ciclo_e_pulado(self):
        self.em(20)
        self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6}, {})
        self.assertEqual(self.enviadas(), [])

    def test_desligado_no_config(self):
        self.config["evening_alert"] = {"enabled": False}
        self.em(20)
        self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6},
                                              self.payloads)
        self.assertEqual(self.enviadas(), [])

    def test_falha_no_envio_nao_marca_o_dia(self):
        from tests.apoio import Resposta
        self.requests.roteador_post = lambda url, payload: Resposta({}, status=500)
        self.em(20)
        self.monitor.maybe_send_evening_alert(self.conn, self.config, {PARQUE: 6},
                                              self.payloads)
        self.assertFalse(self.monitor.janela_enviada(self.conn, self.hoje(), PARQUE))

    def test_comando_mostra_o_perfil_e_nao_chama_a_api(self):
        antes = len(self.requests.gets)
        texto = self.monitor.handle_command(
            f"/janela {PARQUE}", self.conn, self.config, {PARQUE: 6})
        self.assertIn("Pico às", texto)
        self.assertIn("20h", texto)
        self.assertEqual(len(self.requests.gets), antes)

    def test_comando_admite_quando_nao_da_para_afirmar(self):
        self.conn.execute("DELETE FROM wait_times")
        self.conn.commit()
        self.assertIn("Ainda não dá para afirmar",
                      self.monitor.handle_command("/janela " + PARQUE, self.conn,
                                                  self.config, {PARQUE: 6}))


class TestDiscoNoHealth(BaseTeste):
    def test_health_mostra_espaco_livre(self):
        texto = self.monitor.format_health(self.conn, self.config, {})
        self.assertIn("Disco:", texto)
        self.assertIn("GB livres", texto)

    def test_alerta_quando_o_disco_aperta(self):
        pouco = type("Uso", (), {"free": 1_000_000_000, "used": 37_000_000_000,
                                 "total": 38_000_000_000})
        with patch("monitor.shutil.disk_usage", return_value=pouco):
            self.assertIn("⚠️", self.monitor.format_health(self.conn, self.config, {}))

    def test_disco_indisponivel_nao_derruba_o_health(self):
        with patch("monitor.shutil.disk_usage", side_effect=OSError("sem acesso")):
            self.assertIn("indisponível",
                          self.monitor.format_health(self.conn, self.config, {}))


class TestHealthcheckDaAPI(unittest.TestCase):
    """O container da API herdava o healthcheck que mede a coleta do monitor."""

    def test_ok_com_resposta_valida(self):
        with patch("healthcheck_api.urllib.request.urlopen") as urlopen:
            resposta = urlopen.return_value.__enter__.return_value
            resposta.status = 200
            resposta.read.return_value = b'{"ok": true, "service": "fila-disney-api"}'
            self.assertEqual(healthcheck_api.main(), 0)

    def test_falha_com_status_errado(self):
        with patch("healthcheck_api.urllib.request.urlopen") as urlopen:
            resposta = urlopen.return_value.__enter__.return_value
            resposta.status = 503
            self.assertEqual(healthcheck_api.main(), 1)

    def test_falha_quando_ninguem_atende(self):
        with patch("healthcheck_api.urllib.request.urlopen",
                   side_effect=OSError("connection refused")):
            self.assertEqual(healthcheck_api.main(), 1)

    def test_falha_com_corpo_sem_ok(self):
        with patch("healthcheck_api.urllib.request.urlopen") as urlopen:
            resposta = urlopen.return_value.__enter__.return_value
            resposta.status = 200
            resposta.read.return_value = b'{"erro": "banco fora"}'
            self.assertEqual(healthcheck_api.main(), 1)


if __name__ == "__main__":
    unittest.main()
