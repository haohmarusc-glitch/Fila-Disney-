"""O roteiro.json do site contra o park_days da watchlist — regra 17 nas duas pontas.

O roteiro que a família consulta no site e o calendário que dispara os alertas
têm que ser o MESMO calendário. Divergência aqui é o bot alertando o parque
errado no dia — que é pior que não alertar.
"""
import json
import unittest


def carregar():
    with open("site/roteiro.json", encoding="utf-8") as f:
        roteiro = json.load(f)
    with open("watchlist.json", encoding="utf-8") as f:
        watchlist = json.load(f)
    return roteiro, watchlist


class TestRoteiroDoSite(unittest.TestCase):
    def setUp(self):
        self.roteiro, self.watchlist = carregar()
        self.dias = {d["data"]: d for d in self.roteiro["dias"]}

    def test_todo_dia_de_parque_do_site_esta_no_park_days(self):
        for data, dia in self.dias.items():
            if dia["parque"] is None:
                continue
            with self.subTest(data=data):
                self.assertIn(data, self.watchlist["park_days"],
                              f"{data} tem parque no site mas não no park_days")
                self.assertIn(dia["parque"], self.watchlist["park_days"][data])

    def test_todo_park_days_esta_no_site(self):
        """A regra vale nos dois sentidos: dia de alerta sem página no site
        seria roteiro invisível para a família."""
        for data, parques in self.watchlist["park_days"].items():
            for parque in parques:
                with self.subTest(data=data):
                    self.assertIn(data, self.dias)
                    self.assertEqual(self.dias[data]["parque"], parque)

    def test_nome_de_parque_existe_na_watchlist(self):
        """Nome errado aqui quebraria o botão 'ver filas' em silêncio."""
        for dia in self.dias.values():
            if dia["parque"] is not None:
                self.assertIn(dia["parque"], self.watchlist["parks"])

    def test_a_viagem_inteira_esta_coberta(self):
        datas = sorted(self.dias)
        self.assertEqual(datas[0], "2026-10-12")
        self.assertEqual(datas[-1], "2026-10-25")
        self.assertEqual(len(datas), 14, "um cartão por dia, sem buraco")

    def test_dias_sem_parque_sao_os_de_descanso(self):
        sem_parque = {d for d, v in self.dias.items() if v["parque"] is None}
        self.assertEqual(sem_parque, {"2026-10-12", "2026-10-16", "2026-10-18",
                                      "2026-10-22", "2026-10-23", "2026-10-24",
                                      "2026-10-25"})
