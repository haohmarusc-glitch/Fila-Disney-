"""Nomes reais do Queue-Times contra a watchlist.

Os nomes abaixo foram copiados da resposta de produção de
`https://queue-times.com/parks/334/queue_times.json` em 23/08/2026. O símbolo de
marca no MEIO do nome ("Mario Kart™: Bowser's Challenge") quebrava o match por
pedaço e a atração sumia inteira — sem alerta, fora do /status e do /perto, sem
nenhum erro aparecer. Faltavam 59 dias para o dia do Epic Universe.
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
        self.assertNotIn("Single Rider", texto, "fila paralela não entra no /status")

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


if __name__ == "__main__":
    unittest.main()
