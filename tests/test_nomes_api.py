"""Nomes reais do Queue-Times contra a watchlist.

Todos os nomes deste arquivo foram copiados da resposta de produção da API em
23/08/2026 — nada aqui é inventado. Cinco atrações estavam invisíveis para o
bot, cada uma por um motivo diferente de pontuação, e nenhuma gerava erro:
nome não casado é indistinguível de atração fora da watchlist.

    Mario Kart™: Bowser's Challenge      ™ no meio, antes dos dois-pontos
    Buzz Lightyear’s Space Ranger Spin   apóstrofo curvo, não o reto
    Rock ’n’ Roller Coaster Starring…    idem, dois deles
    TRANSFORMERS™ The Ride-3D            ™, sem dois-pontos, hífen no 3D
    Soarin' Across America               o nome mudou de verdade

As quatro primeiras a normalização resolve. A última não: a watchlist passou a
guardar só "Soarin'", que sobrevive à próxima troca de filme.

Faltavam 50 dias para a viagem, e as cinco caíam em quatro dias de parque.
"""
import unittest

from tests.apoio import BaseTeste

# Exatamente como a API devolve, com ™ e travessão.
EPIC_UNIVERSE = [
    "Constellation Carousel",
    "Stardust Racers",
    "Stardust Racers Single Rider",
    "Curse of the Werewolf",
    "Curse of the Werewolf Single Rider",
    "Monsters Unchained: The Frankenstein Experiment",
    "Dragon Racer's Rally",
    "Fyre Drill",
    "Hiccup's Wing Gliders",
    "Meet Toothless and Friends",
    "Bowser Jr. Challenge",
    "Mario Kart™: Bowser's Challenge",
    "Mario Kart™: Bowser's Challenge Single Rider",
    "Mine-Cart Madness™",
    "Mine-Cart Madness™ Single Rider",
    "Yoshi's Adventure™",
    "Harry Potter and the Battle at the Ministry™",
    "Harry Potter and the Battle at the Ministry™ Single Rider",
]
PARQUE = "Universal Epic Universe"


def payload_com(nomes):
    return {"lands": [{"name": "Land", "rides": [
        {"id": i, "name": n, "is_open": True, "wait_time": 30}
        for i, n in enumerate(nomes)
    ]}]}


class TestSimboloDeMarca(BaseTeste):
    def setUp(self):
        super().setUp()
        self.park_cfg = self.config["parks"][PARQUE]

    def test_mario_kart_casa_apesar_do_simbolo_no_meio(self):
        casado = self.monitor.nome_watchlist(
            self.park_cfg, "Mario Kart™: Bowser's Challenge")
        self.assertEqual(casado, "Mario Kart: Bowser's Challenge")

    def test_simbolo_no_fim_continua_casando(self):
        for api, esperado in (
            ("Mine-Cart Madness™", "Mine-Cart Madness"),
            ("Harry Potter and the Battle at the Ministry™",
             "Harry Potter and the Battle at the Ministry"),
        ):
            with self.subTest(api=api):
                self.assertEqual(self.monitor.nome_watchlist(self.park_cfg, api), esperado)

    def test_toda_a_watchlist_do_epic_universe_casa_com_a_api(self):
        casados = {self.monitor.nome_watchlist(self.park_cfg, nome)
                   for nome in EPIC_UNIVERSE}
        casados.discard(None)
        faltando = set(self.park_cfg["attractions"]) - casados
        self.assertFalse(faltando, f"atração da watchlist que a API não casa: {faltando}")

    def test_single_rider_continua_fora_mesmo_com_simbolo(self):
        for nome in EPIC_UNIVERSE:
            if "Single Rider" in nome:
                with self.subTest(nome=nome):
                    self.assertIsNone(self.monitor.nome_watchlist(self.park_cfg, nome))

    def test_threshold_chega_na_atracao_certa(self):
        self.assertEqual(
            self.monitor.get_threshold(self.park_cfg, "Mario Kart™: Bowser's Challenge"),
            self.park_cfg["attractions"]["Mario Kart: Bowser's Challenge"],
        )

    def test_status_lista_a_atracao_que_sumia(self):
        texto = self.monitor.format_status(
            PARQUE, payload_com(EPIC_UNIVERSE), self.config)
        self.assertIn("Mario Kart", texto)
        # O bloco de single rider do /status exige conn com histórico; aqui não
        # há, então nem o bloco nem o nome cru da API podem aparecer.
        self.assertNotIn("Single Rider", texto, "nome cru da API não vai para a tela")

    def test_normalizacao_nao_junta_atracoes_diferentes(self):
        # Stardust Racers e Constellation Carousel não podem casar entre si só
        # porque a normalização deixou os nomes mais parecidos.
        self.assertEqual(
            self.monitor.nome_watchlist(self.park_cfg, "Stardust Racers"), "Stardust Racers")
        self.assertEqual(
            self.monitor.nome_watchlist(self.park_cfg, "Constellation Carousel"),
            "Constellation Carousel")

    def test_atracao_fora_da_watchlist_continua_fora(self):
        for nome in ("Fyre Drill", "Bowser Jr. Challenge", "Yoshi's Adventure™"):
            with self.subTest(nome=nome):
                self.assertIsNone(self.monitor.nome_watchlist(self.park_cfg, nome))


# Nomes reais dos outros parques, colhidos da mesma resposta de produção. Cada
# um quebrava por um motivo diferente: apóstrofo curvo, símbolo de marca no meio
# do nome, e pontuação que a API simplesmente não usa.
OUTROS_PARQUES = {
    "Disney Magic Kingdom": [
        ("Buzz Lightyear\u2019s Space Ranger Spin", "Buzz Lightyear's Space Ranger Spin"),
    ],
    "Disney Hollywood Studios": [
        ("Rock \u2019n\u2019 Roller Coaster Starring The Muppets", "Rock 'n' Roller Coaster"),
    ],
    "Universal Studios At Universal Orlando": [
        ("TRANSFORMERS\u2122 The Ride-3D", "Transformers: The Ride 3D"),
    ],
    "Epcot": [
        # O nome mudou de verdade: "Around the World" virou "Across America".
        # Nenhuma normalização resolveria — a watchlist é que passou a guardar
        # só "Soarin'", que sobrevive à próxima troca de filme.
        ("Soarin' Across America", "Soarin'"),
    ],
}


class TestNomesDosOutrosParques(BaseTeste):
    def test_cada_nome_real_casa_com_a_watchlist(self):
        for parque, casos in OUTROS_PARQUES.items():
            park_cfg = self.config["parks"][parque]
            for nome_api, esperado in casos:
                with self.subTest(parque=parque, api=nome_api):
                    self.assertEqual(
                        self.monitor.nome_watchlist(park_cfg, nome_api), esperado)

    def test_single_rider_do_rock_n_roller_continua_fora(self):
        park_cfg = self.config["parks"]["Disney Hollywood Studios"]
        self.assertIsNone(self.monitor.nome_watchlist(
            park_cfg, "Rock 'n' Roller Coaster Starring Aerosmith Single Rider"))

    def test_marca_nao_vira_letra_na_normalizacao(self):
        # NFKD decompoe "\u2122" em "TM" e grudaria as letras no nome. Se um dia
        # alguem trocar NFD por NFKD, este teste cai.
        self.assertEqual(
            self.monitor.normalizar_nome_api("TRANSFORMERS\u2122 The Ride-3D"),
            "transformers the ride 3d")

    def test_apostrofo_curvo_e_reto_dao_o_mesmo_resultado(self):
        self.assertEqual(
            self.monitor.normalizar_nome_api("Rock \u2019n\u2019 Roller Coaster"),
            self.monitor.normalizar_nome_api("Rock 'n' Roller Coaster"))

    def test_nomes_diferentes_nao_colidem(self):
        # A normalização apaga pontuação; não pode fazer atrações distintas
        # casarem entre si.
        mk = self.config["parks"]["Disney Magic Kingdom"]
        self.assertEqual(
            self.monitor.nome_watchlist(mk, "Space Mountain"), "Space Mountain")
        self.assertEqual(
            self.monitor.nome_watchlist(mk, "Big Thunder Mountain Railroad"),
            "Big Thunder Mountain Railroad")
        self.assertIsNone(self.monitor.nome_watchlist(mk, "Dumbo the Flying Elephant"))


if __name__ == "__main__":
    unittest.main()
