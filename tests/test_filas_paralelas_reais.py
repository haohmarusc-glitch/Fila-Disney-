"""As 19 filas paralelas que a Queue-Times publica, com os nomes verbatim.

Colhidas da API na VPS em 23/08/2026, junto com 30 dias de histórico. Este
arquivo existe por dois motivos.

O primeiro é guardar a regra 10: nenhuma dessas entradas pode entrar na
watchlist pelo match parcial. Foi pontuação em nome de API que deixou cinco
atrações invisíveis antes, uma por dia de parque da viagem — o mesmo erro do
outro lado faria "Test Track Presented by Chevrolet Single Rider" virar
"Test Track" e alertar 0 min.

O segundo é registrar por que não adianta tentar exibir essas filas. Houve uma
tentativa de mostrá-las num bloco à parte do /status, revelando só as que o
histórico provasse vivas. A medição está em MEDICAO_30_DIAS: ~18.000 leituras,
nenhuma acima de zero. O critério funcionou; a resposta dele foi "não há fila
viva nenhuma".
"""
import unittest

from tests.apoio import BaseTeste

# Nomes exatamente como a API devolve — ™, ®, "!" e travessão inclusive.
FILAS_POR_PARQUE = {
    "Disney Magic Kingdom": [],
    "Epcot": [
        "Test Track Presented by Chevrolet Single Rider",
        "Remy's Ratatouille Adventure Single Rider",
    ],
    "Disney Hollywood Studios": [
        "Millennium Falcon: Smugglers Run Single Rider",
        "Star Wars: Rise of the Resistance Single Rider",
        "Rock 'n' Roller Coaster Starring Aerosmith Single Rider",
    ],
    "Disney Animal Kingdom": [
        "Expedition Everest - Legend of the Forbidden Mountain Single Rider",
    ],
    "Universal Studios At Universal Orlando": [
        "Revenge of the Mummy™ Single Rider",
        "Fast & Furious - Supercharged™ Single Rider",
        "Harry Potter and the Escape from Gringotts™ Single Rider",
        "MEN IN BLACK™ Alien Attack!™ Single Rider",
    ],
    "Islands Of Adventure At Universal Orlando": [
        "Doctor Doom's Fearfall® Single Rider",
        "The Incredible Hulk Coaster® Single Rider",
        "Hagrid's Magical Creatures Motorbike Adventure™ Single Rider",
        "Harry Potter and the Forbidden Journey™ Single Rider",
    ],
    "Universal Epic Universe": [
        "Stardust Racers Single Rider",
        "Curse of the Werewolf Single Rider",
        "Mario Kart™: Bowser's Challenge Single Rider",
        "Mine-Cart Madness™ Single Rider",
        "Harry Potter and the Battle at the Ministry™ Single Rider",
    ],
}

# (leituras, leituras com is_open, maior wait_time visto) em 30 dias, 23/08/2026.
# A terceira coluna é a que encerra o assunto: zero em todas, sem exceção.
MEDICAO_30_DIAS = {
    "Test Track Presented by Chevrolet Single Rider": (972, 356, 0),
    "Remy's Ratatouille Adventure Single Rider": (972, 548, 0),
    "Millennium Falcon: Smugglers Run Single Rider": (971, 589, 0),
    "Star Wars: Rise of the Resistance Single Rider": (971, 582, 0),
    "Rock 'n' Roller Coaster Starring Aerosmith Single Rider": (971, 586, 0),
    "Expedition Everest - Legend of the Forbidden Mountain Single Rider": (971, 433, 0),
    "Revenge of the Mummy™ Single Rider": (963, 963, 0),
    "Fast & Furious - Supercharged™ Single Rider": (963, 963, 0),
    "Harry Potter and the Escape from Gringotts™ Single Rider": (963, 956, 0),
    "MEN IN BLACK™ Alien Attack!™ Single Rider": (963, 945, 0),
    "Doctor Doom's Fearfall® Single Rider": (963, 915, 0),
    "The Incredible Hulk Coaster® Single Rider": (963, 963, 0),
    "Hagrid's Magical Creatures Motorbike Adventure™ Single Rider": (963, 889, 0),
    "Harry Potter and the Forbidden Journey™ Single Rider": (963, 955, 0),
    "Stardust Racers Single Rider": (594, 5, 0),
    "Curse of the Werewolf Single Rider": (971, 939, 0),
    "Mario Kart™: Bowser's Challenge Single Rider": (971, 955, 0),
    "Mine-Cart Madness™ Single Rider": (971, 909, 0),
    "Harry Potter and the Battle at the Ministry™ Single Rider": (971, 956, 0),
}

TODAS = [nome for nomes in FILAS_POR_PARQUE.values() for nome in nomes]


class TestRegra10ComOsNomesReais(BaseTeste):
    def test_todas_sao_reconhecidas_como_fila_paralela(self):
        for nome in TODAS:
            with self.subTest(fila=nome):
                self.assertTrue(self.monitor.fila_paralela(nome))

    def test_nenhuma_entra_na_watchlist_pelo_match_parcial(self):
        """O risco concreto: 'Test Track ... Single Rider' virar 'Test Track'."""
        for parque, nomes in FILAS_POR_PARQUE.items():
            park_cfg = self.config["parks"][parque]
            for nome in nomes:
                with self.subTest(fila=nome):
                    self.assertIsNone(self.monitor.nome_watchlist(park_cfg, nome))
                    self.assertIsNone(self.monitor.get_threshold(park_cfg, nome))

    def test_nenhuma_entra_em_ranking_nem_no_status(self):
        parque = "Universal Epic Universe"
        payload = {"lands": [{"name": "Dark Universe", "rides": (
            [{"name": n, "wait_time": 0, "is_open": True}
             for n in FILAS_POR_PARQUE[parque]]
            + [{"name": "Stardust Racers", "wait_time": 45, "is_open": True}]
        )}]}
        texto = self.monitor.format_status(parque, payload, self.config, self.conn)
        menores = self.monitor.menores_filas(payload, self.config, parque, 20,
                                             apenas_watchlist=False)
        maiores = self.monitor.maiores_filas(payload, self.config, 20)
        self.assertNotIn("Single Rider", texto)
        for nome in FILAS_POR_PARQUE[parque]:
            with self.subTest(fila=nome):
                self.assertNotIn(nome, [r[1] for r in menores])
                self.assertNotIn(nome, [r[1] for r in maiores])


class TestPorQueNaoDaParaExibir(unittest.TestCase):
    """A medição que encerrou a tentativa de mostrar single rider no /status."""

    def test_nenhuma_fila_paralela_reportou_tempo_acima_de_zero(self):
        maiores = {n: m for n, (_q, _a, m) in MEDICAO_30_DIAS.items() if m}
        self.assertEqual(maiores, {},
                         "se a API passar a publicar tempo, vale reabrir o assunto")

    def test_a_medicao_cobre_as_dezenove(self):
        self.assertEqual(set(MEDICAO_30_DIAS), set(TODAS))
        self.assertEqual(len(TODAS), 19)

    def test_is_open_do_universal_fica_presto_em_true(self):
        """963 de 963 leituras abertas — nenhum parque opera 24h."""
        travadas = [n for n, (q, a, _m) in MEDICAO_30_DIAS.items() if q == a]
        self.assertTrue(travadas, "é o que desqualifica mostrar aberto/fechado")


if __name__ == "__main__":
    unittest.main()
