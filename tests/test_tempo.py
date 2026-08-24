"""Horário de operação medido e duração da atração.

Duas perguntas diferentes: "até quando dá para ir" e "quanto tempo isso me
custa". Nenhuma das duas a Queue-Times responde — ela entrega só `id`, `name`,
`is_open`, `wait_time` e `last_updated`.
"""
import datetime as dt
import json
import unittest

from tests.apoio import BaseTeste

PARQUE = "Epcot"
ATRACOES = ["Test Track", "Frozen Ever After", "Soarin'", "Mission: SPACE"]


class BaseHorario(BaseTeste):
    def gravar_operacao(self, horas_abertas, dias=3, por_hora=6):
        """Grava o parque aberto só nas horas locais indicadas."""
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        linhas = []
        for dia in range(1, dias + 1):
            data = hoje - dt.timedelta(days=dia)
            for hora_local in range(24):
                aberto = int(hora_local in horas_abertas)
                local = dt.datetime.combine(data, dt.time(hora_local), tzinfo=fuso)
                utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(por_hora):
                    ts = (utc + dt.timedelta(minutes=i * 5)).isoformat()
                    for atracao in ATRACOES:
                        linhas.append((ts, PARQUE, "L", atracao,
                                       30 if aberto else None, aberto))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()


class TestHorarioMedido(BaseHorario):
    def test_acha_abertura_e_fechamento(self):
        self.gravar_operacao(range(9, 22))  # 09h às 21h
        self.assertEqual(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE), (9, 21))

    def test_horario_esticado_e_detectado(self):
        """Em outubro os parques esticam por causa das festas de Halloween."""
        self.gravar_operacao(range(9, 24))
        self.assertEqual(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE), (9, 23))

    def test_sem_historico_nao_inventa(self):
        self.assertIsNone(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE))

    def test_hora_com_poucas_leituras_e_ignorada(self):
        self.gravar_operacao(range(9, 22), dias=1, por_hora=1)
        self.assertIsNone(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE))

    def test_fila_paralela_nao_faz_a_madrugada_parecer_operacao(self):
        """As do Universal reportam aberto 963 de 963 leituras — 24h por dia."""
        self.gravar_operacao(range(9, 22))
        esperado = self.monitor.horario_operacao(self.conn, self.config, PARQUE)
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        linhas = []
        for dia in range(1, 4):
            for hora_local in range(24):
                local = dt.datetime.combine(hoje - dt.timedelta(days=dia),
                                            dt.time(hora_local), tzinfo=fuso)
                utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(6):
                    ts = (utc + dt.timedelta(minutes=i * 5)).isoformat()
                    for nome in ("Test Track Presented by Chevrolet Single Rider",
                                 "Frozen Ever After Single Rider"):
                        linhas.append((ts, PARQUE, "L", nome, 0, 1))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()
        self.assertEqual(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE), esperado,
            "fila paralela presa em aberto não pode virar horário de operação")

    def test_status_mostra_o_horario(self):
        self.gravar_operacao(range(9, 22))
        payload = {"lands": [{"name": "L", "rides": [
            {"name": "Test Track", "wait_time": 40, "is_open": True}]}]}
        texto = self.monitor.format_status(PARQUE, payload, self.config, self.conn)
        self.assertIn("09h–21h", texto)
        self.assertIn("pelo histórico", texto, "é o observado, não o oficial do parque")


class TestCorteRelativoAoPico(BaseHorario):
    """O corte fixo de 25% deixava a madrugada passar raspando.

    Fração de atrações abertas por hora, medida no Hollywood Studios de produção
    em 24/08/2026. A madrugada dá EXATAMENTE 25% — 180 de 720 leituras, hora após
    hora — e o corte era `>= 0.25`, então o horário saía (0, 23), o dia inteiro,
    e a hora de fechamento nunca era excluída da previsão.

    Os 25% são cinco shows: Beauty and the Beast, Disney Jr., Frozen Sing-Along,
    Indiana Jones e Little Mermaid. Show tem sessão, não fila, e a API publica
    is_open verdadeiro 24h.
    """

    FRACOES_REAIS_HS = {
        0: .25, 1: .25, 2: .25, 3: .25, 4: .25, 5: .25, 6: .25, 7: .25,
        8: .44, 9: .91, 10: .94, 11: .92, 12: .91, 13: .91, 14: .93, 15: .93,
        16: .85, 17: .83, 18: .78, 19: .80, 20: .76, 21: .69, 22: .28, 23: .25,
    }

    def gravar_fracoes(self, fracoes, atracoes=20, dias=3, por_hora=4):
        """Grava o parque com a fração de atrações abertas pedida em cada hora."""
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        linhas = []
        for dia in range(1, dias + 1):
            data = hoje - dt.timedelta(days=dia)
            for hora_local, fracao in fracoes.items():
                abertas = round(fracao * atracoes)
                local = dt.datetime.combine(data, dt.time(hora_local), tzinfo=fuso)
                utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(por_hora):
                    ts = (utc + dt.timedelta(minutes=i * 5)).isoformat()
                    for n in range(atracoes):
                        aberta = int(n < abertas)
                        linhas.append((ts, PARQUE, "L", f"Atracao {n}",
                                       30 if aberta else None, aberta))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()

    def test_madrugada_de_shows_nao_vira_horario_de_operacao(self):
        self.gravar_fracoes(self.FRACOES_REAIS_HS)
        self.assertEqual(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE), (9, 21),
            "com o corte fixo de 25% isto voltava (0, 23)")

    def test_parque_sem_shows_nao_e_afetado(self):
        """Onde a madrugada zera, o resultado é o mesmo de antes."""
        limpo = {h: (0.0 if h < 9 or h > 21 else 0.9) for h in range(24)}
        self.gravar_fracoes(limpo)
        self.assertEqual(
            self.monitor.horario_operacao(self.conn, self.config, PARQUE), (9, 21))

    def test_piso_absoluto_continua_valendo(self):
        """Metade de um pico baixo ficaria abaixo do que o código chama operar."""
        fraco = {h: (0.05 if h < 9 else 0.30) for h in range(24)}
        self.gravar_fracoes(fraco, atracoes=20)
        horario = self.monitor.horario_operacao(self.conn, self.config, PARQUE)
        self.assertEqual(horario, (9, 23),
                         "0,30 passa no piso de 0,25; 0,05 não passa em nada")


class TestFechamentoForaDaPrevisao(BaseHorario):
    def test_melhor_do_dia_nao_aponta_a_hora_de_fechamento(self):
        """A fila drena na última hora: 10 min ali não é dica, é parque fechando."""
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        perfil = {9: 30, 12: 60, 15: 55, 18: 50, 21: 5}  # 21h drenando
        linhas = []
        for dia in range(1, 4):
            data = hoje - dt.timedelta(days=dia)
            for hora_local in range(24):
                aberto = int(9 <= hora_local <= 21)
                local = dt.datetime.combine(data, dt.time(hora_local), tzinfo=fuso)
                utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(8):
                    ts = (utc + dt.timedelta(minutes=i * 5)).isoformat()
                    linhas.append((ts, PARQUE, "L", "Test Track",
                                   perfil.get(hora_local, 45) if aberto else None,
                                   aberto))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()
        previsao = self.monitor.previsao_por_atracao(self.conn, self.config, PARQUE)
        self.assertTrue(previsao)
        _ride, _abertura, melhor, _pico, _n = previsao[0]
        self.assertNotEqual(melhor[0], 21,
                            "21h é a hora de fechamento, com a fila drenando")


class TestLinhaDaAbertura(BaseHorario):
    """A hora e o número da abertura têm que ser a mesma coisa."""

    def gravar_curva(self, por_hora_local):
        fuso = self.monitor.fuso_do_parque(self.config)
        hoje = self.monitor.utc_now().date()
        linhas = []
        for dia in range(1, 5):
            data = hoje - dt.timedelta(days=dia)
            for hora in range(24):
                aberto = hora in por_hora_local
                utc = dt.datetime.combine(data, dt.time(hora), tzinfo=fuso
                                          ).astimezone(dt.timezone.utc).replace(tzinfo=None)
                for i in range(10):
                    ts = (utc + dt.timedelta(minutes=i * 5)).isoformat()
                    for atracao in ATRACOES:
                        linhas.append((ts, PARQUE, "L", atracao,
                                       por_hora_local.get(hora) if aberto else None,
                                       int(aberto)))
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, ?)", linhas)
        self.conn.commit()

    def test_o_numero_da_abertura_e_o_da_hora_de_abertura(self):
        """Antes vinha a média de 09h+10h com rótulo de 09h: 46 min onde eram 32."""
        self.gravar_curva({9: 30, 10: 60, 11: 60, 12: 60, 13: 50, 14: 40})
        previsao = self.monitor.previsao_por_atracao(self.conn, self.config, PARQUE)
        self.assertTrue(previsao)
        _ride, (hora, minutos), _melhor, _pico, _n = previsao[0]
        self.assertEqual(hora, 9)
        self.assertAlmostEqual(minutos, 30, 0,
                               "o número tem que ser o da hora que o rótulo diz")

    def test_melhor_do_dia_na_abertura_nao_repete_a_hora(self):
        self.gravar_curva({9: 20, 10: 50, 11: 60, 12: 55, 13: 45, 14: 40})
        texto = self.monitor.format_daily_summary(self.conn, self.config, PARQUE)
        self.assertIn("melhor do dia é o próprio rope drop", texto)
        self.assertNotIn("melhor do dia 09h", texto)

    def test_melhor_do_dia_fora_da_abertura_mostra_a_hora(self):
        self.gravar_curva({9: 50, 10: 60, 11: 60, 12: 55, 13: 30, 14: 40})
        texto = self.monitor.format_daily_summary(self.conn, self.config, PARQUE)
        self.assertIn("melhor do dia 13h", texto)
        self.assertNotIn("rope drop ·", texto)


class TestDuracao(BaseTeste):
    def escrever(self, dados):
        self.monitor.DURACOES_PATH.write_text(
            json.dumps({"rides": dados}), encoding="utf-8")

    def test_arquivo_ausente_desativa_sem_quebrar(self):
        self.assertEqual(self.monitor.carregar_duracoes(), {})

    def test_le_e_resolve_pelo_nome_da_watchlist(self):
        self.escrever({PARQUE: {"Test Track": 5, "Frozen Ever After": 5}})
        duracoes = self.monitor.carregar_duracoes()
        self.assertEqual(
            self.monitor.duracao_da_atracao(duracoes, PARQUE, "Test Track"), 5)

    def test_atracao_sem_entrada_fica_sem_duracao(self):
        """Regra 12: sem dado real, nada de estimativa."""
        self.escrever({PARQUE: {"Test Track": 5}})
        duracoes = self.monitor.carregar_duracoes()
        self.assertIsNone(
            self.monitor.duracao_da_atracao(duracoes, PARQUE, "Soarin'"))

    def test_valor_invalido_nao_vira_zero(self):
        self.escrever({PARQUE: {"Test Track": 0, "Soarin'": "cinco",
                                "Mission: SPACE": None, "Frozen Ever After": 5}})
        duracoes = self.monitor.carregar_duracoes()
        for nome in ("Test Track", "Soarin'", "Mission: SPACE"):
            with self.subTest(nome=nome):
                self.assertIsNone(
                    self.monitor.duracao_da_atracao(duracoes, PARQUE, nome))
        self.assertEqual(
            self.monitor.duracao_da_atracao(duracoes, PARQUE, "Frozen Ever After"), 5)

    def test_json_quebrado_nao_derruba_o_status(self):
        self.monitor.DURACOES_PATH.write_text("{ isto nao e json", encoding="utf-8")
        self.assertEqual(self.monitor.carregar_duracoes(), {})

    def test_status_mostra_a_duracao(self):
        payload = {"lands": [{"name": "L", "rides": [
            {"name": "Test Track", "wait_time": 40, "is_open": True}]}]}
        texto = self.monitor.format_status(
            PARQUE, payload, self.config, self.conn, {PARQUE: {"Test Track": 5}})
        self.assertIn("🎬 atração ~5 min", texto)

    def test_status_sem_duracao_nao_inventa(self):
        payload = {"lands": [{"name": "L", "rides": [
            {"name": "Test Track", "wait_time": 40, "is_open": True}]}]}
        texto = self.monitor.format_status(PARQUE, payload, self.config, self.conn, {})
        self.assertNotIn("🎬", texto)


class TestCabeAntesDeFechar(BaseTeste):
    def em(self, hora, minuto=0):
        fuso = self.monitor.fuso_do_parque(self.config)
        return dt.datetime.now(fuso).replace(hour=hora, minute=minuto, second=0)

    def test_cabe_com_folga(self):
        self.assertTrue(self.monitor.cabe_antes_de_fechar(self.em(18), 21, 45))

    def test_nao_cabe_no_fim_do_dia(self):
        self.assertFalse(self.monitor.cabe_antes_de_fechar(self.em(21, 30), 21, 45))

    def test_fechamento_e_a_ultima_hora_com_operacao(self):
        """21h medido significa portão às 22h, não às 21h em ponto."""
        self.assertTrue(self.monitor.cabe_antes_de_fechar(self.em(21, 0), 21, 50))


if __name__ == "__main__":
    unittest.main()


class TestPreShow(unittest.TestCase):
    """Pré-show/embarque = total (TouringPlans) menos veículo (Wikipédia).

    Duas medidas com fonte, em seções rotuladas — nunca no mesmo campo. O pré
    só aparece quando as duas existem e a diferença passa de ruído.
    """

    DURACOES = {"Disney Hollywood Studios": {"Star Wars: Rise of the Resistance": 18,
                                             "Slinky Dog Dash": 3},
                "Disney Animal Kingdom": {"Kilimanjaro Safaris": 20}}
    VEICULOS = {"Disney Hollywood Studios": {"Star Wars: Rise of the Resistance": 7},
                "Disney Animal Kingdom": {"Kilimanjaro Safaris": 21}}

    def _pre(self, park, ride):
        import monitor
        return monitor.pre_da_atracao(self.DURACOES, self.VEICULOS, park, ride)

    def test_total_menos_veiculo(self):
        self.assertEqual(self._pre("Disney Hollywood Studios",
                                   "Star Wars: Rise of the Resistance"), 11)

    def test_sem_veiculo_nao_ha_pre(self):
        """Slinky tem total mas não tem ciclo medido: sem pré, nunca estimado."""
        self.assertIsNone(self._pre("Disney Hollywood Studios", "Slinky Dog Dash"))

    def test_diferenca_negativa_e_divergencia_de_fonte_nao_pre(self):
        """Kilimanjaro: 20 de total contra 21 de ciclo. As fontes divergem na
        folga; inventar um pré aqui seria pior que calar."""
        self.assertIsNone(self._pre("Disney Animal Kingdom", "Kilimanjaro Safaris"))

    def test_diferenca_de_um_minuto_e_ruido(self):
        import monitor
        self.assertIsNone(monitor.pre_da_atracao(
            {"P": {"A": 4}}, {"P": {"A": 3}}, "P", "A"))

    def test_secao_veiculo_do_arquivo_e_valida(self):
        """Toda chave da seção `veiculo` aponta atração real da watchlist."""
        import json
        import monitor
        dados = json.load(open("duracoes.json"))
        watchlist = monitor.load_config()["parks"]
        self.assertTrue(dados.get("veiculo"), "a seção precisa existir")
        for parque, itens in dados["veiculo"].items():
            for atracao, minutos in itens.items():
                with self.subTest(atracao=atracao):
                    self.assertIn(atracao, watchlist[parque]["attractions"])
                    self.assertIsInstance(minutos, int)
                    self.assertGreater(minutos, 0)


class TestNomeDaApiVersusArquivo(BaseTeste):
    """A API decora o nome; o duracoes.json guarda o canônico. Visto em produção.

    Em 24/08/2026 o /status real mostrou a Tower of Terror sem a linha de
    duração mesmo com 15 min no arquivo: a API manda "The Twilight Zone™ Tower
    of Terror" e o lookup era cru. O threshold nunca sofreu disso porque o
    get_threshold sempre passou pelo nome_watchlist.
    """

    def _status(self, nome_api):
        import json
        self.monitor.DURACOES_PATH.write_text(json.dumps({
            "rides": {"Disney Hollywood Studios": {"Tower of Terror": 15}},
            "veiculo": {},
        }), encoding="utf-8")
        payload = {"lands": [{"name": "L", "rides": [
            {"id": 1, "name": nome_api, "is_open": True, "wait_time": 30,
             "last_updated": self.monitor.utc_now().isoformat()}]}]}
        return self.monitor.format_status("Disney Hollywood Studios", payload,
                                          self.config, self.conn)

    def test_nome_decorado_da_api_ainda_acha_a_duracao(self):
        texto = self._status("The Twilight Zone™ Tower of Terror")
        self.assertIn("atração ~15 min", texto)

    def test_nome_igual_ao_canonico_continua_funcionando(self):
        self.assertIn("atração ~15 min", self._status("Tower of Terror"))


class TestContextoNoMenores(BaseTeste):
    """A duração entra no /menores como contexto, nunca como critério.

    "10 min de fila" muda de sentido conforme sejam 10 por um passeio de 20 min
    ou 10 por 90 segundos — sem o parêntese o ranking escondia a diferença. A
    ORDEM continua sendo a fila (regra 12: duração não ordena nada).
    """

    def _menores(self, filas, duracoes_arquivo):
        import json
        self.monitor.DURACOES_PATH.write_text(json.dumps(
            {"rides": {"Disney Animal Kingdom": duracoes_arquivo}, "veiculo": {}}),
            encoding="utf-8")
        payload = {"lands": [{"name": "L", "rides": [
            {"id": i, "name": n, "is_open": True, "wait_time": w,
             "last_updated": self.monitor.utc_now().isoformat()}
            for i, (n, w) in enumerate(filas.items())]}]}
        return self.monitor.format_menores("Disney Animal Kingdom", payload,
                                           self.config, 10)

    def test_duracao_aparece_entre_parenteses(self):
        texto = self._menores({"Kilimanjaro Safaris": 30},
                              {"Kilimanjaro Safaris": 20})
        self.assertIn("(~20 min de atração)", texto)

    def test_a_ordem_continua_sendo_a_fila(self):
        """O passeio de 20 min NÃO sobe por causa da duração."""
        texto = self._menores(
            {"Kilimanjaro Safaris": 30, "TriceraTop Spin": 10},
            {"Kilimanjaro Safaris": 20})
        self.assertLess(texto.index("TriceraTop"), texto.index("Kilimanjaro"))

    def test_sem_duracao_nao_ha_parenteses(self):
        """Regra 15: sem o dado, sem contexto — nunca "~0 min"."""
        texto = self._menores({"TriceraTop Spin": 10}, {})
        self.assertNotIn("min de atração", texto)
        self.assertNotIn("~0", texto)

    def test_nome_decorado_da_api_acha_a_duracao(self):
        """Mesma lição do /status: a API decora, o arquivo guarda o canônico."""
        texto = self._menores(
            {"Expedition Everest - Legend of the Forbidden Mountain": 25},
            {"Expedition Everest": 4})
        self.assertIn("(~4 min de atração)", texto)


class TestPlaceholderForaDoMenores(BaseTeste):
    """Atração que nunca teve fila não é oportunidade — é placeholder.

    Primeiro teste de mesa, 24/08/2026: o /menores do Animal Kingdom gastou as
    dez linhas com trilha e show a 0 min (Tree of Life: 1.226 leituras, máximo
    0). Mesmo padrão da regra 10, mas o nome não denuncia — quem decide é o
    histórico, e por isso o filtro é medido, não uma lista de nomes.
    """

    PARQUE = "Disney Animal Kingdom"

    def _historico(self, ride, leituras, maximo):
        ts = self.monitor.utc_now().isoformat()
        self.conn.executemany(
            "INSERT INTO wait_times (ts, park, land, ride, wait_time, is_open) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            [(ts, self.PARQUE, "L", ride, maximo if i == 0 else 0)
             for i in range(leituras)])
        self.conn.commit()

    def _menores(self, filas):
        payload = {"lands": [{"name": "L", "rides": [
            {"id": i, "name": n, "is_open": True, "wait_time": w,
             "last_updated": self.monitor.utc_now().isoformat()}
            for i, (n, w) in enumerate(filas.items())]}]}
        return self.monitor.format_menores(self.PARQUE, payload, self.config,
                                           10, self.conn)

    def test_maximo_zero_em_amostra_grande_sai_do_ranking(self):
        self._historico("Tree of Life", 600, 0)
        self._historico("Kali River Rapids", 600, 65)
        texto = self._menores({"Tree of Life": 0, "Kali River Rapids": 20})
        self.assertNotIn("Tree of Life", texto)
        self.assertIn("Kali River Rapids", texto)

    def test_atracao_nova_com_poucas_leituras_fica(self):
        """Máximo 0 com amostra pequena não prova placeholder — pode ser
        atração recém-aberta, e escondê-la puniria a novidade."""
        self._historico("Brinquedo Novo", 50, 0)
        self.assertIn("Brinquedo Novo", self._menores({"Brinquedo Novo": 0}))

    def test_quem_ja_teve_fila_fica_mesmo_a_zero_agora(self):
        """Gorilla Falls a 0 min AGORA é oportunidade real: o máximo dela é 5."""
        self._historico("Gorilla Falls Exploration Trail", 600, 5)
        self.assertIn("Gorilla Falls",
                      self._menores({"Gorilla Falls Exploration Trail": 0}))

    def test_sem_conexao_nao_filtra_nada(self):
        """O formatador sem conn (ensaios) degrada para o comportamento antigo."""
        payload = {"lands": [{"name": "L", "rides": [
            {"id": 1, "name": "Tree of Life", "is_open": True, "wait_time": 0,
             "last_updated": self.monitor.utc_now().isoformat()}]}]}
        texto = self.monitor.format_menores(self.PARQUE, payload, self.config, 10)
        self.assertIn("Tree of Life", texto)
