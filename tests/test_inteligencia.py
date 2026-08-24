"""Regressões dos comandos operacionais e das proteções de precisão."""
import datetime as dt

from tests.apoio import BaseTeste, Resposta

EDT = dt.timezone(dt.timedelta(hours=-4))
PARK = "Disney Hollywood Studios"


def payload(rides):
    return {"lands": [{"name": "L", "rides": rides}]}


class TestEstadoDoParque(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, tzinfo=EDT)

    def test_abertura_geral_nao_e_quebra(self):
        p = payload([{"name": f"R{i}", "wait_time": 0, "is_open": False}
                     for i in range(20)])
        self.assertEqual(self.monitor.estado_parque_payload(p), "fechado")
        self.assertIn("fechado ou ainda abrindo",
                      self.monitor.format_fechadas(self.conn, self.config, PARK, p))

    def test_feed_majoritariamente_obsoleto_e_desconhecido(self):
        self.monitor.utc_now = lambda: dt.datetime(2026, 10, 13, 18)
        p = payload([{"name": f"R{i}", "wait_time": 0, "is_open": False,
                      "last_updated": "2026-10-13T15:00:00Z"} for i in range(20)])
        self.assertEqual(self.monitor.estado_parque_payload(p), "desconhecido")

    def test_reabertura_so_alerta_se_parque_ja_operava(self):
        ride = {"name": "Slinky Dog Dash", "wait_time": 20, "is_open": True}
        self.monitor.vigiar_atracao(self.conn, PARK, "Slinky Dog Dash")
        self.monitor.maybe_alertar_reabertura(
            self.conn, self.config, PARK, ride, 0, False, "operando")
        self.assertEqual(self.enviadas(), [])
        self.monitor.maybe_alertar_reabertura(
            self.conn, self.config, PARK, ride, 0, True, "operando")
        self.assertEqual(len(self.enviadas()), 1)
        self.assertIn("REABRIU", self.enviadas()[0])


class TestNovosComandos(BaseTeste):
    def setUp(self):
        super().setUp()
        self.parques = {n: i for i, n in enumerate(self.config["parks"], 1)}
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, tzinfo=EDT)
        self.p = payload([
            {"name": "Slinky Dog Dash", "wait_time": 55, "is_open": True},
            {"name": "Toy Story Mania!", "wait_time": 25, "is_open": True},
            {"name": "Mickey & Minnie's Runaway Railway", "wait_time": 35, "is_open": True},
            {"name": "Star Wars: Rise of the Resistance", "wait_time": 70, "is_open": True},
            {"name": "Alien Swirling Saucers", "wait_time": 0, "is_open": False},
        ])
        self.requests.roteador = lambda _url: Resposta(self.p)

    def cmd(self, texto, coords=None):
        return self.monitor.handle_command(texto, self.conn, self.config,
                                           self.parques, coords)

    def test_vigiar_listar_e_cancelar(self):
        self.assertIn("Vou vigiar", self.cmd("/vigiar Slinky"))
        self.assertIn("Slinky Dog", self.cmd("/vigiar"))
        self.assertIn("removida", self.cmd("/vigiar cancelar Slinky"))

    def test_confianca_exige_amostra_e_nao_promete_fila_real(self):
        r = self.cmd("/confianca Slinky")
        self.assertIn("12 leituras", r)
        base = dt.datetime(2026, 10, 6, 18)  # terça, 14h EDT
        for leitura in range(12):
            self.gravar(PARK, "Slinky Dog Dash", 30 + leitura % 3 * 5,
                        base + dt.timedelta(minutes=5 * leitura))
        r = self.cmd("/confianca Slinky")
        self.assertIn("Faixa comum", r)
        self.assertIn("não mede a espera real", r)

    def test_lotacao_e_estimativa_nao_contagem(self):
        r = self.cmd("/lotacao Hollywood")
        self.assertIn("Lotação estimada", r)
        self.assertIn("não é contagem de pessoas", r)

    def test_parque_fechado_nao_vira_lotacao_leve(self):
        """Visto na VPS em 24/08 às 19h43: o Animal Kingdom já tinha fechado e
        o site anunciava "lotação leve". Show e trilha publicam 0 permanente e
        continuam is_open, então a média das "abertas" dava 0 — que o
        formatador lê como parque vazio, e não como parque fechado.
        """
        shows = ["Festival of the Lion King", "Feathered Friends in Flight!"]
        base = dt.datetime(2026, 10, 6, 18)
        for nome in shows:
            for leitura in range(self.monitor.MIN_LEITURAS_PLACEHOLDER):
                self.gravar(PARK, nome, 0, base + dt.timedelta(minutes=5 * leitura))
        # Tudo fechado menos os shows: é o parque depois do último ciclo.
        self.p = payload(
            [{"name": nome, "wait_time": 0, "is_open": True} for nome in shows]
            + [{"name": "Slinky Dog Dash", "wait_time": 0, "is_open": False}])
        self.requests.roteador = lambda _url: Resposta(self.p)
        r = self.cmd("/lotacao Hollywood")
        self.assertIn("sem filas atuais suficientes", r)
        self.assertNotIn("leve", r)

    def test_lotacao_real_continua_sendo_calculada(self):
        """A correção não pode calar o comando quando há fila de verdade."""
        r = self.cmd("/lotacao Hollywood")
        self.assertIn("Lotação estimada", r)
        self.assertNotIn("sem filas atuais suficientes", r)

    def test_chuva_so_mostra_lista_conservadora(self):
        r = self.cmd("/chuva Hollywood")
        self.assertIn("Mickey", r)
        self.assertIn("Toy Story", r)
        self.assertNotIn("Slinky", r)

    def test_plano_exige_gps_recente_e_nao_repete_atracao(self):
        coords = {"parks": {PARK: [28.35, -81.56]}, "rides": {PARK: {
            "Slinky Dog Dash": [28.3501, -81.5601],
            "Toy Story Mania!": [28.3502, -81.5602],
            "Mickey & Minnie's Runaway Railway": [28.3503, -81.5603],
            "Star Wars: Rise of the Resistance": [28.3504, -81.5604],
        }}}
        self.assertIn("localização recente", self.cmd("/plano Hollywood", coords))
        self.monitor.guardar_localizacao(self.conn, 28.35, -81.56)
        r = self.cmd("/plano Hollywood", coords)
        self.assertIn("Plano dinâmico", r)
        self.assertEqual(r.count("Slinky Dog Dash"), 1)

    def test_multiplos_parques_nao_escolhe_o_primeiro(self):
        self.monitor.is_alert_day = lambda _c: [PARK, "Epcot"]
        r = self.cmd("/status")
        self.assertIn("mais de um parque", r)


class TestPrecisaoAuxiliar(BaseTeste):
    def test_degrau_igual_ao_p90_nao_vira_excepcional(self):
        perfil = {"p25": 5, "mediana": 5, "p75": 10, "p90": 10}
        self.assertEqual(self.loc.classificar_fila(10, perfil),
                         "🟠 acima do normal")
        self.assertEqual(self.loc.classificar_fila(15, perfil),
                         "🔥 excepcionalmente grande")

    def test_localizacao_expira(self):
        self.monitor.utc_now = lambda: dt.datetime(2026, 10, 13, 18)
        self.monitor.guardar_localizacao(self.conn, 1, 2)
        self.assertEqual(self.monitor.ultima_localizacao(self.conn), (1, 2))
        self.monitor.utc_now = lambda: dt.datetime(2026, 10, 13, 22)
        self.assertIsNone(self.monitor.ultima_localizacao(self.conn))

    def test_localizacao_fica_isolada_por_familiar(self):
        self.monitor.guardar_localizacao(self.conn, 1, 2, chat_id=101)
        self.monitor.guardar_localizacao(self.conn, 3, 4, chat_id=202)
        self.assertEqual(self.monitor.ultima_localizacao(self.conn, chat_id=101), (1, 2))
        self.assertEqual(self.monitor.ultima_localizacao(self.conn, chat_id=202), (3, 4))

    def test_park_to_park_padrao_seguro_e_configuracao_da_viagem_ativa(self):
        self.assertFalse(self.loc.config_park_to_park({})["enabled"])
        self.assertTrue(self.loc.config_park_to_park(self.config)["enabled"])

    def test_park_to_park_reconhece_hogwarts_com_marca(self):
        usf = "Universal Studios At Universal Orlando"
        ioa = "Islands Of Adventure At Universal Orlando"
        atual = payload([
            {"name": "E.T. Adventure", "wait_time": 80, "is_open": True},
            {"name": "Hogwarts Express™ - King's Cross Station",
             "wait_time": 5, "is_open": True},
        ])
        outro = payload([
            {"name": "The Amazing Adventures of Spider-Man",
             "wait_time": 5, "is_open": True},
        ])
        coords = {"rides": {
            usf: {"E.T. Adventure": [28.4795, -81.4704]},
            ioa: {"The Amazing Adventures of Spider-Man": [28.4742, -81.4728]},
        }, "park_to_park": {"stations": {
            usf: [28.4794, -81.4703], ioa: [28.4741, -81.4727],
        }}}
        troca = self.loc.avaliar_troca_park_to_park(
            (28.4794, -81.4703), usf, atual, outro, self.config, coords, self.conn)
        self.assertIsNotNone(troca)
        self.assertEqual(troca["park"], ioa)

    def test_contorno_rejeita_falso_positivo_dentro_do_raio_antigo(self):
        coords = {"rides": {"Parque": {
            "A": [28.000, -81.000], "B": [28.000, -80.998],
            "C": [28.002, -80.998], "D": [28.002, -81.000],
        }}}
        self.assertEqual(self.loc.parque_mais_proximo((28.001, -80.999), coords), "Parque")
        self.assertIsNone(self.loc.parque_mais_proximo((28.015, -80.999), coords))
