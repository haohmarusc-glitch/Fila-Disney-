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
                self.assertEqual(dia.get("parques", [dia["parque"]]),
                                 self.watchlist["park_days"][data])

    def test_todo_park_days_esta_no_site(self):
        """A regra vale nos dois sentidos: dia de alerta sem página no site
        seria roteiro invisível para a família."""
        for data, parques in self.watchlist["park_days"].items():
            for parque in parques:
                with self.subTest(data=data):
                    self.assertIn(data, self.dias)
                    dia = self.dias[data]
                    self.assertIn(parque, dia.get("parques", [dia["parque"]]))

    def test_nome_de_parque_existe_na_watchlist(self):
        """Nome errado aqui quebraria o botão 'ver filas' em silêncio."""
        for dia in self.dias.values():
            if dia["parque"] is not None:
                for parque in dia.get("parques", [dia["parque"]]):
                    self.assertIn(parque, self.watchlist["parks"])

    def test_a_viagem_inteira_esta_coberta(self):
        datas = sorted(self.dias)
        self.assertEqual(datas[0], "2026-10-12")
        self.assertEqual(datas[-1], "2026-10-25")
        self.assertEqual(len(datas), 14, "um cartão por dia, sem buraco")

    def test_dias_sem_parque_sao_os_de_descanso(self):
        sem_parque = {d for d, v in self.dias.items() if v["parque"] is None}
        self.assertEqual(sem_parque, {"2026-10-12", "2026-10-16",
                                      "2026-10-22", "2026-10-23", "2026-10-24",
                                      "2026-10-25"})

    def test_cronograma_universal_da_familia(self):
        """Calendário mestre FIRME v11, de 06/10/2026.

        O v11 trocou 20 com 21 e tirou o Park-to-Park do dia 19. A versão
        anterior (12/09) tinha 19 = Islands + USF, 20 = USF e 21 = Epic.
        """
        ioa = "Islands Of Adventure At Universal Orlando"
        usf = "Universal Studios At Universal Orlando"
        epic = "Universal Epic Universe"
        self.assertEqual(self.watchlist["park_days"]["2026-10-18"], [ioa])
        self.assertEqual(self.watchlist["park_days"]["2026-10-19"], [usf])
        self.assertEqual(self.watchlist["park_days"]["2026-10-20"], [epic])
        self.assertEqual(self.watchlist["park_days"]["2026-10-21"], [ioa])

    def test_cronograma_disney_da_familia(self):
        """O v11 também trocou 13 com 14: o Animal Kingdom passou para terça."""
        self.assertEqual(self.watchlist["park_days"]["2026-10-13"],
                         ["Disney Animal Kingdom"])
        self.assertEqual(self.watchlist["park_days"]["2026-10-14"],
                         ["Disney Hollywood Studios"])
        self.assertEqual(self.watchlist["park_days"]["2026-10-15"], ["Epcot"])
        self.assertEqual(self.watchlist["park_days"]["2026-10-17"],
                         ["Disney Magic Kingdom"])

    def test_nenhum_dia_do_v11_tem_dois_parques(self):
        """O Park-to-Park saiu no v11; a chave `parques` não sobrou em lugar nenhum."""
        for data, dia in self.dias.items():
            with self.subTest(data=data):
                self.assertNotIn("parques", dia)
        for data, parques in self.watchlist["park_days"].items():
            with self.subTest(data=data):
                self.assertEqual(len(parques), 1)

    def test_detalhes_das_fotos_substituem_o_planejamento_antigo(self):
        jantar = self.dias["2026-10-18"]["timeline"]
        self.assertTrue(any(p["hora"] == "19h30" and "California Grill" in p["texto"]
                            for p in jantar))
        self.assertIn("ainda não comprado", self.dias["2026-10-20"]["furafila"])
        self.assertIn("Express Pass", self.dias["2026-10-20"]["destaque"])
        self.assertTrue(any(p["hora"] == "26/10 · 00h20"
                            for p in self.dias["2026-10-25"]["timeline"]))
