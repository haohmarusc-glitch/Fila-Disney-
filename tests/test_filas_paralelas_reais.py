"""As 19 filas paralelas que a Queue-Times publica, com os nomes verbatim.

Colhidas da API na VPS em 23/08/2026. O casamento por normalização já falhou em
silêncio antes — cinco atrações da watchlist ficaram invisíveis por pontuação e
símbolo de marca no nome, uma por dia de parque da viagem. Aqui o mesmo risco
existe do outro lado: se `atracao_da_fila_paralela` parar de casar, a fila some
do /status sem nenhum erro no log.
"""
import unittest

from tests.apoio import BaseTeste

# Nomes exatamente como a API devolve — ™, ®, "!" e travessão inclusive.
FILAS_POR_PARQUE = {
    "Disney Magic Kingdom": {},
    "Epcot": {
        "Test Track Presented by Chevrolet Single Rider": "Test Track",
        "Remy's Ratatouille Adventure Single Rider": "Remy's Ratatouille Adventure",
    },
    "Disney Hollywood Studios": {
        "Millennium Falcon: Smugglers Run Single Rider": "Millennium Falcon: Smugglers Run",
        "Star Wars: Rise of the Resistance Single Rider": "Star Wars: Rise of the Resistance",
        "Rock 'n' Roller Coaster Starring Aerosmith Single Rider": "Rock 'n' Roller Coaster",
    },
    "Disney Animal Kingdom": {
        "Expedition Everest - Legend of the Forbidden Mountain Single Rider":
            "Expedition Everest",
    },
    "Universal Studios At Universal Orlando": {
        "Revenge of the Mummy™ Single Rider": "Revenge of the Mummy",
        "Harry Potter and the Escape from Gringotts™ Single Rider":
            "Harry Potter and the Escape from Gringotts",
        "MEN IN BLACK™ Alien Attack!™ Single Rider": "MEN IN BLACK Alien Attack",
        # Fora da watchlist de propósito: a família não planeja essa atração.
        "Fast & Furious - Supercharged™ Single Rider": None,
    },
    "Islands Of Adventure At Universal Orlando": {
        "Doctor Doom's Fearfall® Single Rider": "Doctor Doom's Fearfall",
        "The Incredible Hulk Coaster® Single Rider": "The Incredible Hulk Coaster",
        "Hagrid's Magical Creatures Motorbike Adventure™ Single Rider":
            "Hagrid's Magical Creatures Motorbike Adventure",
        "Harry Potter and the Forbidden Journey™ Single Rider":
            "Harry Potter and the Forbidden Journey",
    },
    "Universal Epic Universe": {
        "Stardust Racers Single Rider": "Stardust Racers",
        "Curse of the Werewolf Single Rider": "Curse of the Werewolf",
        "Mario Kart™: Bowser's Challenge Single Rider": "Mario Kart: Bowser's Challenge",
        "Mine-Cart Madness™ Single Rider": "Mine-Cart Madness",
        "Harry Potter and the Battle at the Ministry™ Single Rider":
            "Harry Potter and the Battle at the Ministry",
    },
}

# O ROTEIRO conta com single rider no IOA em 19/10 ("sem Express: rope drop +
# single rider"). É o dia em que o bloco do /status mais importa.
IOA = "Islands Of Adventure At Universal Orlando"


class TestCasamentoComANomenclaturaReal(BaseTeste):
    def test_toda_fila_paralela_casa_com_a_atracao_certa(self):
        for parque, esperado in FILAS_POR_PARQUE.items():
            park_cfg = self.config["parks"][parque]
            for nome_api, atracao in esperado.items():
                with self.subTest(parque=parque, fila=nome_api):
                    self.assertEqual(
                        self.monitor.atracao_da_fila_paralela(park_cfg, nome_api),
                        atracao)

    def test_todas_sao_reconhecidas_como_fila_paralela(self):
        for esperado in FILAS_POR_PARQUE.values():
            for nome_api in esperado:
                with self.subTest(fila=nome_api):
                    self.assertTrue(self.monitor.fila_paralela(nome_api))

    def test_nenhuma_entra_na_watchlist_pela_porta_normal(self):
        """Regra 10: o match parcial não pode fazer a fila virar a atração real."""
        for parque, esperado in FILAS_POR_PARQUE.items():
            park_cfg = self.config["parks"][parque]
            for nome_api in esperado:
                with self.subTest(fila=nome_api):
                    self.assertIsNone(self.monitor.nome_watchlist(park_cfg, nome_api))
                    self.assertIsNone(self.monitor.get_threshold(park_cfg, nome_api))

    def test_o_dia_do_ioa_tem_as_quatro_filas(self):
        park_cfg = self.config["parks"][IOA]
        casadas = {self.monitor.atracao_da_fila_paralela(park_cfg, nome)
                   for nome in FILAS_POR_PARQUE[IOA]}
        self.assertEqual(casadas, {
            "Hagrid's Magical Creatures Motorbike Adventure",
            "The Incredible Hulk Coaster",
            "Harry Potter and the Forbidden Journey",
            "Doctor Doom's Fearfall",
        }, "é o dia sem Express: o roteiro conta com single rider aqui")

    def test_magic_kingdom_nao_publica_nenhuma(self):
        """TRON e Tiana não têm single rider — nem na Disney, nem na API."""
        self.assertEqual(FILAS_POR_PARQUE["Disney Magic Kingdom"], {})


if __name__ == "__main__":
    unittest.main()
