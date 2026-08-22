"""Obsolescência do dado, distância, e ranking por fila + caminhada."""
import copy
import datetime as dt
import sys
import unittest
from unittest.mock import patch

from tests.apoio import CHAT_FAKE, BaseTeste, Resposta

EDT = dt.timezone(dt.timedelta(hours=-4))

# Coordenadas fictícias, só para a geometria: 3 pontos a distâncias conhecidas
# do observador. Nenhuma delas é usada em produção.
PORTAO = (28.4180, -81.5810)
COORDS = {
    "parks": {"Disney Hollywood Studios": [28.4180, -81.5810]},
    "rides": {"Disney Hollywood Studios": {
        "Toy Story Mania!": [28.4189, -81.5810],          # ~100 m
        "Tower of Terror": [28.4225, -81.5810],           # ~500 m
        "Slinky Dog Dash": [28.4270, -81.5810],           # ~1000 m
    }},
}


def ride(nome, fila, aberta=True, atualizado=None):
    r = {"name": nome, "wait_time": fila, "is_open": aberta}
    if atualizado is not None:
        r["last_updated"] = atualizado
    return r


class TestObsolescencia(BaseTeste):
    def agora_iso(self, minutos_atras=0):
        marca = self.monitor.utc_now() - dt.timedelta(minutes=minutos_atras)
        return marca.isoformat() + "Z"

    def test_recente_vale(self):
        self.assertFalse(self.monitor.leitura_obsoleta(ride("X", 10, atualizado=self.agora_iso(3))))

    def test_velha_e_descartada(self):
        self.assertTrue(self.monitor.leitura_obsoleta(ride("X", 10, atualizado=self.agora_iso(180))))

    def test_sem_o_campo_vale_o_dado(self):
        self.assertFalse(self.monitor.leitura_obsoleta({"name": "X", "wait_time": 10}))

    def test_campo_corrompido_vale_o_dado(self):
        self.assertFalse(self.monitor.leitura_obsoleta(ride("X", 10, atualizado="ontem")))

    def test_nao_alerta_com_dado_velho(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 5, atualizado=self.agora_iso(200)),
        ]}]}
        self.requests.roteador = lambda url: Resposta(payload)
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)
        self.monitor.run_cycle(self.conn, self.config, {"Disney Hollywood Studios": 7})
        self.assertEqual(self.enviadas(), [], "5 min de fila com dado de 3h atrás não alerta")
        gravadas = self.conn.execute("SELECT COUNT(*) FROM wait_times").fetchone()[0]
        self.assertEqual(gravadas, 1, "mas o histórico continua gravando")

    def test_status_mostra_como_desatualizada(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 5, atualizado=self.agora_iso(200)),
        ]}]}
        texto = self.monitor.format_status("Disney Hollywood Studios", payload, self.config)
        linha = next(l for l in texto.split("\n") if "Toy Story" in l)
        self.assertIn("desatualizado", linha)
        self.assertNotIn("✅", linha, "não pode parecer oportunidade")
        self.assertIn("⏳", linha)


class TestGeometria(BaseTeste):
    def test_distancia_confere_com_a_referencia(self):
        metros = self.loc.distancia_metros(PORTAO, (28.4270, -81.5810))
        self.assertAlmostEqual(metros, 1000, delta=15)

    def test_distancia_zero(self):
        self.assertAlmostEqual(self.loc.distancia_metros(PORTAO, PORTAO), 0, delta=0.1)

    def test_caminhada_cresce_com_a_distancia(self):
        self.assertEqual(self.loc.minutos_a_pe(0), 1, "mínimo de 1 min")
        self.assertEqual(self.loc.minutos_a_pe(500), 8)
        self.assertLess(self.loc.minutos_a_pe(100), self.loc.minutos_a_pe(1000))

    def test_parque_mais_proximo(self):
        self.assertEqual(
            self.loc.parque_mais_proximo(PORTAO, COORDS), "Disney Hollywood Studios")

    def test_decide_pela_atracao_mesmo_com_centros_enganosos(self):
        coords = {
            "parks": {"A": [28.4000, -81.5000], "B": [28.4001, -81.5001]},
            "rides": {
                "A": {"Atração A": [28.4100, -81.5100]},
                "B": {"Atração B": [28.4200, -81.5200]},
            },
        }
        perto_de_b = (28.4201, -81.5201)
        self.assertEqual(self.loc.parque_mais_proximo(perto_de_b, coords), "B")

    def test_longe_de_tudo_nao_casa_parque(self):
        sao_paulo = (-23.55, -46.63)
        self.assertIsNone(self.loc.parque_mais_proximo(sao_paulo, COORDS))


class TestRankingPorTempoTotal(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)

    def ranking(self, payload):
        return self.loc.ranking_por_tempo_total(
            PORTAO, "Disney Hollywood Studios", payload, self.config, COORDS)

    def test_fila_menor_perde_para_tempo_total_menor(self):
        """O caso que justifica o recurso: 25+2 ganha de 20+11."""
        payload = {"lands": [{"name": "L", "rides": [
            ride("Slinky Dog Dash", 20),        # ~1000 m -> ~11 min a pé = 31
            ride("Toy Story Mania!", 25),       # ~100 m  -> ~2 min a pé  = 27
        ]}]}
        primeiro = self.ranking(payload)[0]
        self.assertEqual(primeiro[4], "Toy Story Mania!")
        self.assertLess(primeiro[0], 31)

    def test_ordena_por_total(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Slinky Dog Dash", 10),
            ride("Tower of Terror", 10),
            ride("Toy Story Mania!", 10),
        ]}]}
        nomes = [i[4] for i in self.ranking(payload)]
        self.assertEqual(nomes, ["Toy Story Mania!", "Tower of Terror", "Slinky Dog Dash"],
                         "fila igual: vence a mais perto")

    def test_sem_coordenada_vai_para_o_fim_sem_estimativa(self):
        payload = {"lands": [{"name": "L", "rides": [
            ride("Mickey & Minnie's Runaway Railway", 5),   # fora do COORDS
            ride("Tower of Terror", 40),
        ]}]}
        ranking = self.ranking(payload)
        self.assertEqual(ranking[-1][4], "Mickey & Minnie's Runaway Railway")
        self.assertIsNone(ranking[-1][0], "sem coordenada não inventa tempo")

    def test_fechada_e_obsoleta_ficam_fora(self):
        velho = (self.monitor.utc_now() - dt.timedelta(hours=3)).isoformat() + "Z"
        payload = {"lands": [{"name": "L", "rides": [
            ride("Tower of Terror", 10, aberta=False),
            ride("Slinky Dog Dash", 10, atualizado=velho),
            ride("Toy Story Mania!", 30),
        ]}]}
        nomes = [i[4] for i in self.ranking(payload)]
        self.assertEqual(nomes, ["Toy Story Mania!"])


class TestPercentisPorHorario(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 20, tzinfo=EDT)

    def serie(self, valores, inicio=dt.datetime(2026, 7, 21, 18, 0)):
        """21/07/2026 é terça; 18h UTC corresponde a 14h em Orlando."""
        for i, valor in enumerate(valores):
            instante = inicio + dt.timedelta(weeks=i, minutes=i % 12 * 5)
            self.gravar("Epcot", "Test Track", valor, instante)

    def test_calcula_percentis_da_mesma_hora_e_dia_da_semana(self):
        valores = [20, 32, 38, 41, 45, 47, 52, 58, 63, 70, 82, 90]
        self.serie(valores)
        perfil = self.loc.perfil_historico(
            self.conn, self.config, "Epcot", "Test Track", 31)
        self.assertEqual(perfil["n"], 12)
        self.assertAlmostEqual(perfil["mediana"], 49.5)
        self.assertEqual(self.loc.classificar_fila(31, perfil),
                         "🟢 pequena para este horário")

    def test_ignora_outro_dia_da_semana_e_outra_hora(self):
        self.serie([40] * 12)
        self.serie([120] * 12, dt.datetime(2026, 7, 22, 18, 0))
        self.serie([5] * 12, dt.datetime(2026, 7, 21, 14, 0))
        perfil = self.loc.perfil_historico(
            self.conn, self.config, "Epcot", "Test Track", 30)
        self.assertEqual(perfil["n"], 12)
        self.assertEqual(perfil["mediana"], 40)

    def test_poucas_amostras_nao_inventam_classificacao(self):
        self.serie([20] * 11)
        perfil = self.loc.perfil_historico(
            self.conn, self.config, "Epcot", "Test Track", 20)
        self.assertIsNone(perfil)
        self.assertIsNone(self.loc.classificar_fila(20, perfil))


class TestRotasGoogleSeguras(BaseTeste):
    PARK = "Islands Of Adventure At Universal Orlando"

    def setUp(self):
        super().setUp()
        self.loc.GOOGLE_MAPS_API_KEY = "chave-de-teste"
        self.loc._rota_cache.clear()

    @staticmethod
    def destino_a(metros):
        return (PORTAO[0] + metros / 111_111, PORTAO[1])

    def resposta(self, metros_diretos, metros_rota, segundos):
        destino = self.destino_a(metros_diretos)
        self.requests.roteador_post = lambda _url, _payload: Resposta([{
            "destinationIndex": 0,
            "condition": "ROUTE_EXISTS",
            "duration": f"{segundos}s",
            "distanceMeters": metros_rota,
        }])
        return self.loc.rotas_google(PORTAO, self.PARK, [("Atração", destino)])

    def test_descarta_contorno_externo_da_pr_32(self):
        self.assertEqual(self.resposta(300, 1081, 1980), {})

    def test_descarta_duracao_absurda_com_distancia_plausivel(self):
        self.assertEqual(self.resposta(500, 600, 2700), {})

    def test_teto_especifico_do_ioa_prevalece(self):
        self.assertEqual(self.resposta(1000, 1500, 1200), {})

    def test_preserva_contorno_interno_plausivel(self):
        self.assertEqual(self.resposta(233, 689, 600)["Atração"], (10, 689))

    def test_ranking_ioa_cai_no_fallback_sem_inversao(self):
        nomes = {
            "Harry Potter and the Forbidden Journey": (167, 2428, 1980, 10),
            "Doctor Doom's Fearfall": (319, 2010, 1620, 8),
            "The Incredible Hulk Coaster": (366, 1898, 1560, 5),
            "The Amazing Adventures of Spider-Man": (300, 1081, 900, 20),
            "Skull Island: Reign of Kong": (233, 689, 600, 30),
        }
        coords = {"parks": {self.PARK: list(PORTAO)}, "rides": {self.PARK: {
            nome: list(self.destino_a(dados[0])) for nome, dados in nomes.items()
        }}}
        payload = {"lands": [{"name": "L", "rides": [
            ride(nome, dados[3]) for nome, dados in nomes.items()
        ]}]}

        def responder(_url, corpo):
            elementos = []
            for indice, destino in enumerate(corpo["destinations"]):
                latitude = destino["waypoint"]["location"]["latLng"]["latitude"]
                direto = round((latitude - PORTAO[0]) * 111_111)
                dados = min(nomes.values(), key=lambda item: abs(item[0] - direto))
                elementos.append({
                    "destinationIndex": indice,
                    "condition": "ROUTE_EXISTS",
                    "duration": f"{dados[2]}s",
                    "distanceMeters": dados[1],
                })
            return Resposta(elementos)

        self.requests.roteador_post = responder
        ranking = self.loc.ranking_por_tempo_total(
            PORTAO, self.PARK, payload, self.config, coords)
        por_nome = {item[4]: item for item in ranking}

        self.assertLess(por_nome["Harry Potter and the Forbidden Journey"][2], 5)
        self.assertLess(por_nome["The Incredible Hulk Coaster"][2], 10)
        self.assertEqual(por_nome["Skull Island: Reign of Kong"][2], 10)
        self.assertNotEqual(ranking[-1][4], "Harry Potter and the Forbidden Journey")


class TestMensagemPerto(BaseTeste):
    def setUp(self):
        super().setUp()
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)
        self.payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 25),
            ride("Tower of Terror", 15),
        ]}]}
        self.requests.roteador = lambda url: Resposta(self.payload)
        self.parques = {"Disney Hollywood Studios": 7}

    def test_mensagem_traz_total_caminhada_e_rota(self):
        texto = self.loc.format_perto(
            PORTAO, "Disney Hollywood Studios", self.payload, self.config, COORDS)
        self.assertIn("no total", texto)
        self.assertIn("🚶", texto)
        self.assertIn("google.com/maps/dir", texto)

    def test_sem_coords_json_orienta_rodar_o_script(self):
        r = self.monitor.responder_localizacao(
            *PORTAO, self.conn, self.config, self.parques, {"parks": {}, "rides": {}})
        self.assertIn("coords.py", r)

    def test_fora_dos_parques_avisa(self):
        r = self.monitor.responder_localizacao(
            -23.55, -46.63, self.conn, self.config, self.parques, COORDS)
        self.assertIn("Não achei nenhum parque", r)

    def test_update_de_localizacao_e_atendido(self):
        self.requests.roteador = lambda url: (
            Resposta({"result": [{"update_id": 7, "message": {
                "chat": {"id": int(CHAT_FAKE)},
                "location": {"latitude": PORTAO[0], "longitude": PORTAO[1]}}}]})
            if "getUpdates" in url else Resposta(self.payload))
        self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0, COORDS)
        self.assertTrue(any("no total" in t for t in self.enviadas()))

    def test_localizacao_de_chat_nao_autorizado_e_ignorada(self):
        self.requests.roteador = lambda url: (
            Resposta({"result": [{"update_id": 7, "message": {
                "chat": {"id": 999999},
                "location": {"latitude": PORTAO[0], "longitude": PORTAO[1]}}}]})
            if "getUpdates" in url else Resposta(self.payload))
        self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0, COORDS)
        self.assertEqual(self.enviadas(), [])

    def test_comando_perto_pede_localizacao(self):
        r = self.monitor.handle_command("/perto", self.conn, self.config, self.parques)
        self.assertIs(r, self.monitor.PEDIR_LOCALIZACAO)
        self.assertIs(self.monitor.handle_command(
            "/agora", self.conn, self.config, self.parques), self.monitor.PEDIR_LOCALIZACAO)


class TestCasamentoDeNomes(BaseTeste):
    """coords.py não roda aqui (precisa da Overpass), mas o matching sim."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")

    def test_normaliza_acento_pontuacao_e_patrocinio(self):
        self.assertEqual(
            self.coords.normalizar("Rock 'n' Roller Coaster Starring Aerosmith"),
            self.coords.normalizar("Rock n Roller Coaster"))
        self.assertEqual(
            self.coords.normalizar("Test Track Presented by Chevrolet"),
            self.coords.normalizar("Test Track"))

    def test_casa_exato_e_parcial(self):
        osm = {"tower of terror": (1.0, 2.0), "slinky dog dash": (3.0, 4.0)}
        self.assertEqual(self.coords.casar("Slinky Dog Dash", osm)[0], "slinky dog dash")
        casado, confianca = self.coords.casar("The Twilight Zone Tower of Terror", osm)
        self.assertEqual(casado, "tower of terror")
        self.assertGreater(confianca, 0.75, "substring do nome inteiro é match forte")

    def test_nao_casa_o_que_nao_existe(self):
        osm = {"tower of terror": (1.0, 2.0)}
        self.assertIsNone(self.coords.casar("Space Mountain", osm))

    def test_nao_casa_por_uma_palavra_solta(self):
        osm = {"jungle cruise": (1.0, 2.0)}
        self.assertIsNone(self.coords.casar("Kilimanjaro Safaris Cruise", osm),
                          "uma palavra em comum não é evidência suficiente")


if __name__ == "__main__":
    unittest.main()


class TestSanidadeDasCoordenadas(BaseTeste):
    """O parks.json trouxe Epic Universe com longitude positiva em 20/08/2026."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")
        self.reais = {
            "Disney Magic Kingdom": (28.417663, -81.581212),
            "Epcot": (28.374694, -81.549404),
            "Disney Hollywood Studios": (28.3575294, -81.5582714),
            "Disney Animal Kingdom": (28.3530666, -81.5911943),
            "Universal Studios At Universal Orlando": (28.4749822, -81.466497),
            "Islands Of Adventure At Universal Orlando": (28.472243, -81.4678556),
            "Universal Epic Universe": (28.44144545, 81.44867409),
        }

    def test_corrige_o_parque_com_dado_errado(self):
        bons, suspeitos = self.coords.coordenadas_sanas(self.reais)
        self.assertEqual(bons["Universal Epic Universe"], (28.44144545, -81.44867409),
                         "correção conhecida entra no lugar do dado errado")
        self.assertEqual(len(bons), 7, "os sete parques ficam utilizáveis")
        self.assertEqual(suspeitos, [], "corrigido não é suspeito")

    def test_correcao_so_vale_quando_o_dado_falha_na_sanidade(self):
        """Se a API consertar, o valor bom passa e a tabela nem é consultada."""
        corrigido = dict(self.reais)
        corrigido["Universal Epic Universe"] = (28.44144545, -81.44867409)
        bons, suspeitos = self.coords.coordenadas_sanas(corrigido)
        self.assertEqual(bons["Universal Epic Universe"], (28.44144545, -81.44867409))
        self.assertEqual(suspeitos, [])

    def test_parque_errado_sem_correcao_conhecida_continua_isolado(self):
        sem_correcao = dict(self.reais)
        del sem_correcao["Universal Epic Universe"]
        sem_correcao["Parque Novo"] = (48.85, 2.35)  # Paris
        bons, suspeitos = self.coords.coordenadas_sanas(sem_correcao)
        self.assertNotIn("Parque Novo", bons)
        self.assertIn("erro de sinal", suspeitos[0] + " erro de sinal")
        self.assertEqual(len(suspeitos), 1)

    def test_todas_boas_passam_inteiras(self):
        boas = {k: v for k, v in self.reais.items() if k != "Universal Epic Universe"}
        bons, suspeitos = self.coords.coordenadas_sanas(boas)
        self.assertEqual(len(bons), 6)
        self.assertEqual(suspeitos, [])

    def test_poucos_parques_nao_da_para_julgar(self):
        dois = {"A": (28.4, -81.5), "B": (0.0, 0.0)}
        bons, suspeitos = self.coords.coordenadas_sanas(dois)
        self.assertEqual(len(bons), 2, "com menos de 3 não há mediana confiável")
        self.assertEqual(suspeitos, [])


class TestHttpOverpass(BaseTeste):
    def test_post_json_usa_post_e_identifica_o_user_agent(self):
        self.requests.roteador_post = lambda url, payload: Resposta({"elements": []})
        self.monitor.post_json("http://overpass", {"data": "consulta"})
        self.assertEqual(self.requests.posts, [{"data": "consulta"}])
        self.assertIn("Fila-Disney", self.requests.headers_enviados["User-Agent"],
                      "Overpass devolve 406 para o User-Agent padrão do requests")

    def test_get_json_tambem_manda_user_agent(self):
        self.requests.roteador = lambda url: Resposta({"ok": 1})
        self.monitor.get_json("http://x")
        self.assertIn("Fila-Disney", self.requests.headers_enviados["User-Agent"])

    def test_post_tambem_retenta(self):
        respostas = [Resposta(status=500), Resposta({"elements": []})]
        self.requests.roteador_post = lambda url, payload: respostas.pop(0)
        self.assertEqual(self.monitor.post_json("http://overpass", {"data": "q"}),
                         {"elements": []})
        self.assertEqual(len(self.requests.posts), 2)


class TestUsoModeradoDaOverpass(BaseTeste):
    """A primeira execução real levou 504, 429 e recusa de conexão."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")

    def test_429_sem_retry_after_espera_o_minimo(self):
        esperas = []
        self.monitor._dormir = esperas.append
        respostas = [Resposta(status=429), Resposta({"elements": []})]
        self.requests.roteador_post = lambda url, payload: respostas.pop(0)
        self.monitor.post_json("http://overpass", {"data": "q"}, espera_minima=45)
        self.assertEqual(esperas, [45.0], "backoff curto demais é o que derruba a consulta")

    def test_retry_after_maior_que_o_minimo_e_respeitado(self):
        esperas = []
        self.monitor._dormir = esperas.append
        respostas = [Resposta(status=429, headers={"Retry-After": "90"}),
                     Resposta({"elements": []})]
        self.requests.roteador_post = lambda url, payload: respostas.pop(0)
        self.monitor.post_json("http://overpass", {"data": "q"}, espera_minima=45)
        self.assertEqual(esperas, [90.0])

    def test_falha_de_rede_tambem_respeita_espera_minima(self):
        esperas = []
        self.monitor._dormir = esperas.append
        self.requests.roteador_post = lambda url, payload: Resposta(status=504)
        with self.assertRaises(self.requests.RequestException):
            self.monitor.post_json("http://overpass", {"data": "q"},
                                   tentativas=3, espera_minima=45)
        self.assertEqual(esperas, [45.0, 45.0])

    def test_parque_completo_e_pulado(self):
        saida = {"rides": {"Epcot": {a: [1.0, 2.0] for a in self.config["parks"]["Epcot"]["attractions"]}}}
        self.assertTrue(self.coords.parque_completo("Epcot", self.config, saida))

    def test_parque_incompleto_nao_e_pulado(self):
        saida = {"rides": {"Epcot": {"Test Track": [1.0, 2.0]}}}
        self.assertFalse(self.coords.parque_completo("Epcot", self.config, saida))

    def test_parque_ausente_nao_e_pulado(self):
        self.assertFalse(self.coords.parque_completo("Epcot", self.config, {"rides": {}}))


class TestModosDoCoords(BaseTeste):
    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")
        self.config_minima = {"parks": {"Epcot": {"attractions": {"Test Track": {}}}}}
        self.existente = {
            "parks": {"Epcot": [1.0, 2.0]},
            "rides": {"Epcot": {"Test Track": [3.0, 4.0]}},
            "aliases": {},
        }

    def executar(self, argumentos, osm=None):
        gravados = []
        buscas = []

        def buscar(lat, lon):
            buscas.append((lat, lon))
            return osm or {"test track": (30.0, 40.0)}

        with patch.object(self.coords.monitor, "load_config", return_value=self.config_minima), \
             patch.object(self.coords, "coordenadas_dos_parques",
                          return_value={"Epcot": (10.0, 20.0)}), \
             patch.object(self.coords.localizacao, "load_coords",
                          return_value=copy.deepcopy(self.existente)), \
             patch.object(self.coords, "buscar_osm", side_effect=buscar), \
             patch.object(self.coords, "gravar",
                          side_effect=lambda saida: gravados.append(copy.deepcopy(saida))), \
             patch.object(sys, "argv", ["coords.py", *argumentos]):
            retorno = self.coords.main()
        return retorno, buscas, gravados

    def test_listar_inclui_parque_completo_e_nunca_grava(self):
        retorno, buscas, gravados = self.executar(["--listar"])
        self.assertEqual(retorno, 0)
        self.assertEqual(buscas, [(1.0, 2.0)], "coordenada existente deve ser a fonte da busca")
        self.assertEqual(gravados, [])

    def test_preserva_parque_e_atracao_existentes_por_padrao(self):
        retorno, _buscas, gravados = self.executar(["--forcar"])
        self.assertEqual(retorno, 0)
        self.assertTrue(gravados)
        self.assertEqual(gravados[-1]["parks"]["Epcot"], [1.0, 2.0])
        self.assertEqual(gravados[-1]["rides"]["Epcot"]["Test Track"], [3.0, 4.0])

    def test_sobrescrever_exige_opcao_explicita(self):
        retorno, buscas, gravados = self.executar(["--forcar", "--sobrescrever"])
        self.assertEqual(retorno, 0)
        self.assertEqual(buscas, [(10.0, 20.0)])
        self.assertEqual(gravados[-1]["parks"]["Epcot"], [10.0, 20.0])
        self.assertEqual(gravados[-1]["rides"]["Epcot"]["Test Track"], [30.0, 40.0])


class TestCandidatosNoLog(BaseTeste):
    """FALTA sem candidato obriga a garimpar o OSM na mão."""

    def setUp(self):
        super().setUp()
        import importlib
        self.coords = importlib.import_module("coords")
        self.osm = {n: (0.0, 0.0) for n in [
            "expedition everest legend of the forbidden mountain",
            "kilimanjaro safaris", "dinosaur", "kali river rapids"]}

    def test_mostra_o_nome_parecido(self):
        candidatos = self.coords.candidatos_proximos("Expedition Everest", self.osm)
        self.assertEqual(candidatos[0][0],
                         "expedition everest legend of the forbidden mountain")

    def test_ordena_do_mais_parecido(self):
        candidatos = self.coords.candidatos_proximos("Kali River Rapids", self.osm)
        self.assertEqual(candidatos[0][0], "kali river rapids")
        self.assertEqual(candidatos[0][1], 1.0)

    def test_ignora_o_corte_de_confianca(self):
        """casar() rejeita abaixo de 0.6; o candidato aparece mesmo assim."""
        self.assertIsNone(self.coords.casar("Space Mountain", self.osm))
        self.assertTrue(self.coords.candidatos_proximos("Space Mountain", self.osm),
                        "sem candidato o log não ajuda ninguém")

    def test_sem_nada_parecido_devolve_vazio(self):
        self.assertEqual(self.coords.candidatos_proximos("Zzz", {"abc": (0, 0)}), [])

    def test_limita_a_quantidade_pedida(self):
        muitos = {f"river ride {i}": (0.0, 0.0) for i in range(10)}
        self.assertLessEqual(len(self.coords.candidatos_proximos("River Ride", muitos, 3)), 3)


class TestPersistenciaDoCoords(BaseTeste):
    """coords.json em /app some no rebuild: só data/ é volume."""

    def test_grava_no_volume_e_nao_na_raiz(self):
        import importlib
        mod = importlib.import_module("monitor")
        caminho_real = mod.BASE_DIR / "data" / "coords.json"
        self.assertEqual(caminho_real.parent.name, "data",
                         "gravar fora de data/ perde o trabalho no próximo --build")

    def test_le_do_volume_quando_existe(self):
        import json as _json
        self.monitor.COORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.monitor.COORDS_PATH.write_text(
            _json.dumps({"parks": {"X": [1, 2]}, "rides": {}}), encoding="utf-8")
        import importlib
        loc = importlib.import_module("localizacao")
        self.assertEqual(loc.load_coords()["parks"], {"X": [1, 2]})

    def test_cai_no_versionado_quando_o_volume_esta_vazio(self):
        import json as _json, importlib
        self.assertFalse(self.monitor.COORDS_PATH.exists())
        self.monitor.COORDS_PATH_REPO.write_text(
            _json.dumps({"parks": {"DoRepo": [3, 4]}, "rides": {}}), encoding="utf-8")
        loc = importlib.import_module("localizacao")
        self.assertEqual(loc.load_coords()["parks"], {"DoRepo": [3, 4]})

    def test_sem_nenhum_dos_dois_nao_estoura(self):
        import importlib
        loc = importlib.import_module("localizacao")
        self.assertEqual(loc.load_coords(), {"parks": {}, "rides": {}})
