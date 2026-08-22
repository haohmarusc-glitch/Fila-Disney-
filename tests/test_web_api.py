import unittest
import sys
from unittest.mock import patch

from tests.apoio import _requests

sys.modules.setdefault("requests", _requests)
import api_server


class TestParametrosAPI(unittest.TestCase):
    def test_aceita_coordenada_valida(self):
        self.assertEqual(api_server._number({"lat": ["28.47"]}, "lat", -90, 90), 28.47)

    def test_recusa_ausente_infinito_e_fora_da_faixa(self):
        for query in ({}, {"lat": ["inf"]}, {"lat": ["91"]}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                api_server._number(query, "lat", -90, 90)


class TestPayloadPerto(unittest.TestCase):
    @patch("api_server.localizacao.com_score", return_value=[])
    @patch("api_server.localizacao._ranking_detalhado")
    @patch("api_server.monitor.fetch_queue_times", return_value={"lands": []})
    @patch("api_server.localizacao.parque_mais_proximo", return_value="Epcot")
    def test_devolve_json_estruturado(self, _park, _fetch, ranking, _score):
        ranking.return_value = [(17, 10, 7, 500.4, "Test Track", (1.0, 2.0), "google", None)]
        result = api_server.build_perto_payload(1, 2, object(), {}, {"Epcot": 5}, {})
        self.assertEqual(result["park"], "Epcot")
        self.assertEqual(result["items"][0]["total"], 17)
        self.assertEqual(result["items"][0]["route_source"], "google")

    @patch("api_server.localizacao.parque_mais_proximo", return_value=None)
    def test_recusa_local_fora_dos_parques(self, _park):
        with self.assertRaisesRegex(ValueError, "fora dos parques"):
            api_server.build_perto_payload(0, 0, object(), {}, {}, {})


if __name__ == "__main__":
    unittest.main()
