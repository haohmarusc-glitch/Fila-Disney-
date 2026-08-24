"""Cliente HTTP: retry, 429, JSON inválido, API fora do ar."""
import unittest

from tests.apoio import JSON_QUEBRADO, BaseTeste, Resposta, payload_parques


class TestGetJson(BaseTeste):
    def test_sucesso_na_primeira(self):
        self.requests.roteador = lambda url: Resposta({"ok": 1})
        self.assertEqual(self.monitor.get_json("http://x"), {"ok": 1})
        self.assertEqual(len(self.requests.gets), 1)

    def test_retenta_e_vence(self):
        respostas = [Resposta(status=500), Resposta(status=503), Resposta({"ok": 1})]
        self.requests.roteador = lambda url: respostas.pop(0)
        self.assertEqual(self.monitor.get_json("http://x"), {"ok": 1})
        self.assertEqual(len(self.requests.gets), 3)

    def test_desiste_depois_das_tentativas(self):
        self.requests.roteador = lambda url: Resposta(status=500)
        with self.assertRaises(self.requests.RequestException):
            self.monitor.get_json("http://x")
        self.assertEqual(len(self.requests.gets), self.monitor.HTTP_TENTATIVAS)

    def test_429_respeita_retry_after(self):
        esperas = []
        self.monitor._dormir = esperas.append
        respostas = [Resposta(status=429, headers={"Retry-After": "7"}), Resposta({"ok": 1})]
        self.requests.roteador = lambda url: respostas.pop(0)
        self.assertEqual(self.monitor.get_json("http://x"), {"ok": 1})
        self.assertEqual(esperas, [7.0])

    def test_get_texto_devolve_texto_com_o_mesmo_retry(self):
        """Regra 11: HTML não passa pelo `get_json`, e não pode virar exceção.

        O `get_texto` é o irmão, não a fuga — a regra existe pelo retry e pelo
        429, não pelo content-type. Se ele perdesse o núcleo, uma página do
        TouringPlans fora do ar mataria a coleta na primeira tentativa.
        """
        respostas = [Resposta(status=500), Resposta(texto="<html>ok</html>")]
        self.requests.roteador = lambda url: respostas.pop(0)
        self.assertEqual(self.monitor.get_texto("http://x"), "<html>ok</html>")
        self.assertEqual(len(self.requests.gets), 2, "tem que ter retentado")

    def test_get_texto_respeita_o_429(self):
        esperas = []
        self.monitor._dormir = esperas.append
        respostas = [Resposta(status=429, headers={"Retry-After": "7"}),
                     Resposta(texto="ok")]
        self.requests.roteador = lambda url: respostas.pop(0)
        self.assertEqual(self.monitor.get_texto("http://x"), "ok")
        self.assertEqual(esperas, [7.0])

    def test_404_nao_retenta(self):
        self.requests.roteador = lambda url: Resposta(status=404)
        with self.assertRaises(self.requests.RequestException):
            self.monitor.get_json("http://x")
        self.assertEqual(len(self.requests.gets), 1, "4xx não melhora sozinho")

    def test_json_quebrado_vira_falha(self):
        self.requests.roteador = lambda url: Resposta(JSON_QUEBRADO)
        with self.assertRaises(self.requests.RequestException):
            self.monitor.get_json("http://x")


class TestFormatoDaResposta(BaseTeste):
    def test_queue_times_sem_lands_nem_rides_e_recusado(self):
        self.requests.roteador = lambda url: Resposta({"qualquer": "coisa"})
        with self.assertRaises(self.requests.RequestException):
            self.monitor.fetch_queue_times(1)

    def test_parks_json_fora_do_formato_e_recusado(self):
        self.requests.roteador = lambda url: Resposta({"nao": "e lista"})
        with self.assertRaises(self.requests.RequestException):
            self.monitor.resolve_park_ids(["Epcot"])

    def test_ausencia_de_dado_nao_vira_zero(self):
        payload = {"lands": [{"name": "L", "rides": [
            {"name": "Slinky Dog Dash", "wait_time": None, "is_open": True},
            {"name": "Toy Story Mania!", "wait_time": 20, "is_open": True},
        ]}]}
        ranking = self.monitor.menores_filas(
            payload, self.config, "Disney Hollywood Studios", 10, apenas_watchlist=True
        )
        nomes = [nome for _w, nome, _t in ranking]
        self.assertNotIn("Slinky Dog Dash", nomes, "wait_time None não pode virar 0 min")
        self.assertIn("Toy Story Mania!", nomes)


class TestResolucaoDeParques(BaseTeste):
    def test_resolve_nome_exato_e_parcial(self):
        self.requests.roteador = lambda url: Resposta(payload_parques(
            {"Epcot": 5, "Disney Magic Kingdom": 6, "Epic Universe": 334}))
        ids = self.monitor.resolve_park_ids(["Epcot", "Universal Epic Universe"])
        self.assertEqual(ids["Epcot"], 5)
        self.assertEqual(ids["Universal Epic Universe"], 334, "casa por pedaço do nome")

    def test_nome_que_nao_existe_fica_de_fora(self):
        self.requests.roteador = lambda url: Resposta(payload_parques({"Epcot": 5}))
        self.assertEqual(self.monitor.resolve_park_ids(["Legoland"]), {})

    def test_sugestao_aponta_o_nome_certo(self):
        disponiveis = {
            "universal studios at universal orlando": 65,
            "islands of adventure at universal orlando": 64,
            "epcot": 5,
        }
        sugestoes = self.monitor.suggest_park_names("universal studios florida", disponiveis)
        self.assertEqual(sugestoes[0], "universal studios at universal orlando")


if __name__ == "__main__":
    unittest.main()
