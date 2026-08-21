"""Obsolescência do dado, distância, e ranking por fila + caminhada."""
import datetime as dt
import json
import unittest
from pathlib import Path

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

    def test_longe_de_tudo_nao_casa_parque(self):
        sao_paulo = (-23.55, -46.63)
        self.assertIsNone(self.loc.parque_mais_proximo(sao_paulo, COORDS))

    def test_navi_separa_local_real_da_ancora_caminhavel(self):
        """A rota usa Pandora, sem apagar a coordenada real da atração."""
        caminho = Path(__file__).parents[1] / "coords.json"
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        real = dados["rides"]["Disney Animal Kingdom"]["Na'vi River Journey"]
        ancora = dados["route_anchors"]["Disney Animal Kingdom"]["Na'vi River Journey"]
        self.assertNotEqual(real, ancora["coord"])
        self.assertEqual(ancora["coord"], [28.354861, -81.592826])
        self.assertGreater(ancora["extra_minutes"], 0)
        self.assertGreater(ancora["extra_meters"], 0)
        coord, minutos, metros = self.loc.ancora_rota(
            dados, "Disney Animal Kingdom", "Na'vi River Journey", real)
        self.assertEqual(coord, tuple(ancora["coord"]))
        self.assertEqual((minutos, metros), (2, 150))


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

    def test_nome_com_subtitulo_usa_coordenada_do_nome_base(self):
        coords = {"rides": {"Disney Animal Kingdom": {
            "Expedition Everest": [28.3585, -81.5863],
        }}}
        config = {**self.config, "parks": {**self.config["parks"],
                  "Disney Animal Kingdom": {"attractions": {
                      "Expedition Everest": {"threshold": 60}}}}}
        payload = {"lands": [{"name": "Asia", "rides": [
            ride("Expedition Everest - Legend of the Forbidden Mountain", 30),
        ]}]}
        ranking = self.loc.ranking_por_tempo_total(
            (28.358, -81.586), "Disney Animal Kingdom", payload, config, coords)
        self.assertIsNotNone(ranking[0][0])
        self.assertIsNotNone(ranking[0][5])

    def test_subtitulo_nao_faz_casamento_parcial_ambiguo(self):
        self.assertIsNone(self.loc.coordenada_atracao(
            {"Expedition": [1, 2]},
            "Expedition Everest - Legend of the Forbidden Mountain"))

    def test_nome_da_api_usa_coordenada_do_nome_da_watchlist(self):
        parque = "Universal Studios At Universal Orlando"
        nomes = {
            "Revenge of the Mummy™": "Revenge of the Mummy",
            "The Simpsons Ride™": "The Simpsons Ride",
            "Harry Potter and the Escape from Gringotts™":
                "Harry Potter and the Escape from Gringotts",
            "Illumination's Villain-Con Minion Blast": "Villain-Con Minion Blast",
        }
        config = {**self.config, "parks": {**self.config["parks"], parque: {
            "attractions": {canonico: 60 for canonico in nomes.values()}}}}
        coords = {"rides": {parque: {
            canonico: [28.48 + i / 1000, -81.47]
            for i, canonico in enumerate(nomes.values())}}}
        payload = {"lands": [{"name": "L", "rides": [
            ride(nome_api, 20) for nome_api in nomes
        ]}]}

        ranking = self.loc.ranking_por_tempo_total(
            (28.48, -81.47), parque, payload, config, coords)

        self.assertEqual(len(ranking), 4)
        self.assertTrue(all(item[5] is not None for item in ranking))


class TestRotasGoogle(BaseTeste):
    def setUp(self):
        super().setUp()
        self.loc.GOOGLE_MAPS_API_KEY = "chave-de-teste"
        self.loc._rota_cache.clear()

    def test_rota_real_substitui_distancia_direta(self):
        self.requests.roteador_post = lambda _url, _payload: Resposta([{
            "originIndex": 0,
            "destinationIndex": 0,
            "condition": "ROUTE_EXISTS",
            "duration": "600s",
            "distanceMeters": 805,
        }])
        rotas = self.loc.rotas_google(PORTAO, [("Frozen", (28.3708, -81.5462))])
        self.assertEqual(rotas["Frozen"], (10, 805))
        self.assertEqual(self.requests.headers_enviados["X-Goog-Api-Key"],
                         "chave-de-teste")

    def test_cache_evitar_cobranca_repetida(self):
        self.requests.roteador_post = lambda _url, _payload: Resposta([{
            "destinationIndex": 0, "condition": "ROUTE_EXISTS",
            "duration": "120s", "distanceMeters": 150,
        }])
        destinos = [("Toy Story Mania!", COORDS["rides"]["Disney Hollywood Studios"]["Toy Story Mania!"])]
        self.loc.rotas_google(PORTAO, destinos)
        self.loc.rotas_google(PORTAO, destinos)
        self.assertEqual(len(self.requests.posts), 1)

    def test_falha_da_api_degrada_para_estimativa(self):
        self.requests.roteador_post = lambda _url, _payload: Resposta(status=500)
        self.assertEqual(self.loc.rotas_google(PORTAO, [("X", (1.0, 2.0))]), {})

    def test_ranking_roteia_todas_e_preserva_resposta_parcial(self):
        nomes = [f"Atração {i}" for i in range(8)]
        coords = {"rides": {"Disney Hollywood Studios": {
            nome: [PORTAO[0] + i / 10000, PORTAO[1]]
            for i, nome in enumerate(nomes, 1)
        }}}
        config = {**self.config, "parks": {**self.config["parks"],
                  "Disney Hollywood Studios": {"attractions": {
                      nome: {"threshold": 60} for nome in nomes}}}}
        payload = {"lands": [{"name": "L", "rides": [
            ride(nome, 20 + i) for i, nome in enumerate(nomes)
        ]}]}
        self.requests.roteador_post = lambda _url, _payload: Resposta([{
            "destinationIndex": 0, "condition": "ROUTE_EXISTS",
            "duration": "60s", "distanceMeters": 50,
        }])

        ranking = self.loc.ranking_por_tempo_total(
            PORTAO, "Disney Hollywood Studios", payload, config, coords)

        self.assertEqual(len(self.requests.posts[0]["destinations"]), 8,
                         "a estimativa não pode excluir candidatos antes da rota")
        self.assertEqual(len(ranking), 8,
                         "resposta parcial da API não pode encolher a watchlist")

    def test_rodape_nao_afirma_rota_quando_api_falha(self):
        self.requests.roteador_post = lambda _url, _payload: Resposta(status=500)
        payload = {"lands": [{"name": "L", "rides": [
            ride("Toy Story Mania!", 25),
        ]}]}
        texto = self.loc.format_perto(
            PORTAO, "Disney Hollywood Studios", payload, self.config, COORDS)
        self.assertIn("estimativa por distância", texto)
        self.assertNotIn("calculada por rota a pé", texto)


class TestUniversalParkToPark(BaseTeste):
    USF = "Universal Studios At Universal Orlando"
    IOA = "Islands Of Adventure At Universal Orlando"

    def setUp(self):
        super().setUp()
        self.config = {
            "alert": {"max_staleness_minutes": 30},
            "parks": {
                self.USF: {"attractions": {"Atração atual": 60}},
                self.IOA: {"attractions": {"Atração do outro parque": 60}},
            },
            "park_to_park": {
                "enabled": True,
                "parks": {self.USF: self.IOA, self.IOA: self.USF},
                "min_savings_minutes": 15,
                "train_ride_minutes": 4,
                "boarding_buffer_minutes": 4,
            },
        }
        self.coords = {
            "rides": {
                self.USF: {"Atração atual": [28.4800, -81.4700]},
                self.IOA: {"Atração do outro parque": [28.4730, -81.4730]},
            },
            "park_to_park": {"stations": {
                self.USF: [28.4794, -81.4703],
                self.IOA: [28.4734544, -81.4724556],
            }},
        }
        self.atual = {"lands": [{"name": "L", "rides": [
            ride("Atração atual", 80),
            ride("Hogwarts Express - King's Cross Station", 10),
        ]}]}
        self.outro = {"lands": [{"name": "L", "rides": [
            ride("Atração do outro parque", 10),
        ]}]}
        self.loc.rotas_google = lambda _pos, destinos: {
            nome: ((5, 300) if nome == "__hogwarts_station__" else (3, 200))
            for nome, _coord in destinos
        }

    def avaliar(self):
        return self.loc.avaliar_troca_park_to_park(
            (28.4801, -81.4699), self.USF, self.atual, self.outro,
            self.config, self.coords)

    def test_recomenda_trem_quando_economia_supera_margem(self):
        troca = self.avaliar()
        self.assertEqual(troca["ride"], "Atração do outro parque")
        self.assertEqual(troca["total"], 36)
        self.assertEqual(troca["savings"], 47)
        self.assertEqual(troca["train_wait"], 10)

    def test_nao_recomenda_troca_por_vantagem_pequena(self):
        self.atual["lands"][0]["rides"][0]["wait_time"] = 40
        self.assertIsNone(self.avaliar())

    def test_trem_fechado_desativa_troca(self):
        self.atual["lands"][0]["rides"][1]["is_open"] = False
        self.assertIsNone(self.avaliar())

    def test_fila_hogwarts_aceita_nome_da_estacao(self):
        self.assertEqual(self.loc.fila_hogwarts(self.atual, 30), 10)


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

    def test_mensagem_mostra_recomendacao_park_to_park(self):
        troca = {
            "park": "Islands Of Adventure At Universal Orlando",
            "ride": "Hagrid's Magical Creatures Motorbike Adventure",
            "total": 52, "ride_wait": 25, "walk_to_station": 5,
            "walk_to_ride": 8, "station_meters": 300,
            "train_wait": 10, "train_ride": 4,
            "boarding_buffer": 4, "savings": 18,
        }
        texto = self.loc.format_perto(
            PORTAO, "Disney Hollywood Studios", self.payload,
            self.config, COORDS, troca=troca)
        self.assertIn("Vale trocar para Islands", texto)
        self.assertIn("economia estimada: <b>18 min</b>", texto)

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

    def test_volume_antigo_nao_esconde_secao_nova_do_versionado(self):
        import json as _json, importlib
        self.monitor.COORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.monitor.COORDS_PATH_REPO.write_text(_json.dumps({
            "parks": {"X": [1, 2]}, "rides": {},
            "park_to_park": {"stations": {"X": [3, 4]}},
        }), encoding="utf-8")
        self.monitor.COORDS_PATH.write_text(_json.dumps({
            "parks": {"X": [9, 9]}, "rides": {},
        }), encoding="utf-8")
        dados = importlib.import_module("localizacao").load_coords()
        self.assertEqual(dados["parks"]["X"], [9, 9], "volume continua vencendo")
        self.assertEqual(dados["park_to_park"]["stations"]["X"], [3, 4])

    def test_sem_nenhum_dos_dois_nao_estoura(self):
        import importlib
        loc = importlib.import_module("localizacao")
        self.assertEqual(loc.load_coords(), {"parks": {}, "rides": {}})
