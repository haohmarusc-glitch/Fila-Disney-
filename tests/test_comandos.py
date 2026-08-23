"""Comandos do Telegram, autorização e escape de HTML."""
import datetime as dt
import unittest

from tests.apoio import CHAT_FAKE, BaseTeste, Resposta

EDT = dt.timezone(dt.timedelta(hours=-4))

PAYLOAD = {"lands": [{"name": "L", "rides": [
    {"name": "Mickey & Minnie's Runaway Railway", "wait_time": 35, "is_open": True},
    {"name": "Slinky Dog Dash", "wait_time": 55, "is_open": True},
    {"name": "Toy Story Mania!", "wait_time": 25, "is_open": True},
    {"name": "Muppet*Vision 3D", "wait_time": 5, "is_open": True},
    {"name": "Star Tours", "wait_time": 10, "is_open": True},
    {"name": "Rock 'n' Roller Coaster Starring Aerosmith", "wait_time": 85, "is_open": True},
    {"name": "Test Track Presented by Chevrolet Single Rider", "wait_time": 0, "is_open": True},
    {"name": "Indiana Jones Stunt Spectacular", "wait_time": 20, "is_open": False},
]}]}


class BaseComando(BaseTeste):
    def setUp(self):
        super().setUp()
        self.requests.roteador = lambda url: Resposta(PAYLOAD)
        self.parques = {n: i for i, n in enumerate(self.config["parks"], 1)}
        self.monitor.now_park = lambda _c: dt.datetime(2026, 10, 13, 14, 0, tzinfo=EDT)

    def cmd(self, texto):
        return self.monitor.handle_command(texto, self.conn, self.config, self.parques)


class TestRoteamento(BaseComando):
    def test_ajuda_e_aliases(self):
        for texto in ("/help", "/start", "/ajuda"):
            self.assertEqual(self.cmd(texto), self.monitor.HELP, texto)

    def test_comando_desconhecido_devolve_ajuda(self):
        self.assertEqual(self.cmd("/foo"), self.monitor.HELP)

    def test_texto_solto_e_ignorado(self):
        self.assertIsNone(self.cmd("bom dia"))

    def test_mensagem_vazia_e_ignorada(self):
        self.assertIsNone(self.cmd("   \n  "))

    def test_so_a_primeira_linha_vale(self):
        r = self.cmd("/menores Hollywood\n/resumo Hollywood\n/help")
        self.assertIn("Hollywood Studios", r)
        self.assertNotIn("Não achei", r)

    def test_sufixo_do_bot_em_grupo(self):
        self.assertIn("Epcot", self.cmd("/status@FilaBot Epcot"))

    def test_teste_alertas_exige_parque(self):
        self.assertIn("teste_alertas", self.cmd("/teste_alertas"))

    def test_teste_park_to_park_mostra_simulacao_sem_chamar_api(self):
        self.requests.gets.clear()
        resposta = self.cmd("/teste_park_to_park")
        self.assertIn("SIMULAÇÃO Park-to-Park", resposta)
        self.assertIn("Vale trocar para Islands Of Adventure", resposta)
        self.assertIn("economia estimada: <b>21 min</b>", resposta)
        self.assertIn("não usa as filas atuais", resposta)
        self.assertEqual(self.requests.gets, [])


class TestParque(BaseComando):
    def test_atalhos_que_resolvem(self):
        casos = {
            "magic": "Disney Magic Kingdom",
            "epcot": "Epcot",
            "hollywood": "Disney Hollywood Studios",
            "animal": "Disney Animal Kingdom",
            "islands": "Islands Of Adventure At Universal Orlando",
            "studios at": "Universal Studios At Universal Orlando",
            "epic": "Universal Epic Universe",
        }
        for atalho, esperado in casos.items():
            self.assertEqual(self.monitor.match_parks(atalho, self.parques), [esperado], atalho)

    def test_ambiguo_lista_as_opcoes_em_vez_de_escolher(self):
        r = self.cmd("/status universal")
        self.assertIn("mais de um parque", r)
        self.assertNotIn("no horário do parque", r, "não pode escolher sozinho")

    def test_inexistente_orienta(self):
        self.assertIn("/parques", self.cmd("/status Legoland"))


class TestStatusEMenores(BaseComando):
    def test_status_so_watchlist(self):
        r = self.cmd("/status Hollywood")
        self.assertIn("Slinky Dog Dash", r)
        self.assertNotIn("Muppet", r, "/status não mostra fora da watchlist")

    def test_menores_mostra_parque_inteiro(self):
        r = self.cmd("/menores Hollywood")
        self.assertIn("Muppet", r)
        self.assertIn("⭐", r, "marca o que está na watchlist")

    def test_single_rider_fora_dos_dois(self):
        for texto in ("/status Hollywood", "/menores Hollywood"):
            self.assertNotIn("Single Rider", self.cmd(texto), texto)

    def test_fechada_fora_dos_dois(self):
        for texto in ("/status Hollywood", "/menores Hollywood"):
            self.assertNotIn("Indiana Jones", self.cmd(texto), texto)

    def test_escape_de_html(self):
        r = self.cmd("/status Hollywood")
        self.assertIn("Mickey &amp; Minnie", r)
        self.assertNotIn("Mickey & Minnie", r, "& cru faz o Telegram devolver 400")

    def test_api_fora_do_ar_responde_em_vez_de_estourar(self):
        self.requests.roteador = lambda url: Resposta(status=500)
        self.assertIn("Não consegui", self.cmd("/status Hollywood"))

    def test_health_responde_sem_historico(self):
        self.monitor.APP_GIT_SHA = "abc1234"
        r = self.cmd("/health")
        self.assertIn("Monitor de filas", r)
        self.assertIn("nunca", r)
        self.assertIn("abc1234", r)

    def _estado(self):
        return {tabela: self.conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
                for tabela in ("alerts_sent", "top_alert", "daily_summary",
                               "reopen_alerts", "evening_alert", "reminders_sent")}

    def test_ensaio_cobre_todas_as_mensagens_automaticas(self):
        """As seis estreariam na viagem se não fossem ensaiadas antes."""
        antes = self._estado()
        self.assertIsNone(self.cmd("/teste_alertas Hollywood"))
        mensagens = self.enviadas()
        self.assertIn("Ensaio", mensagens[0], "abre dizendo o que vem")
        corpo = mensagens[1:]
        self.assertTrue(all("TESTE" in mensagem for mensagem in corpo))
        for esperado in ("threshold", "Top-3", "reabertura", "resumo das 7h",
                         "janela noturna", "lembrete"):
            with self.subTest(mensagem=esperado):
                self.assertTrue(any(esperado in m for m in corpo),
                                f"o ensaio não cobre {esperado}")
        self.assertEqual(self._estado(), antes,
                         "ensaio não pode marcar cooldown, resumo, janela nem lembrete")

    def test_ensaio_usa_os_formatadores_reais(self):
        """Ensaio com texto copiado não ensaiaria nada."""
        self.cmd("/teste_alertas Hollywood")
        corpo = "\n".join(self.enviadas())
        self.assertIn("REABRIU", corpo)          # format_reabertura
        self.assertIn("Vai agora", corpo)        # notifier.format_alert
        self.assertIn("Powered by Queue-Times.com", corpo)

    def test_ensaio_admite_o_que_nao_da_para_mostrar(self):
        """Sem janela detectada, ele diz isso em vez de omitir a mensagem."""
        self.cmd("/teste_alertas Hollywood")
        corpo = "\n".join(self.enviadas())
        self.assertIn("Ainda não há janela detectada", corpo)


class TestRanking(BaseComando):
    def test_ranking_parque_ordena_maior_primeiro(self):
        r = self.cmd("/ranking Hollywood")
        self.assertLess(r.index("Rock 'n' Roller"), r.index("Slinky Dog Dash"))
        self.assertNotIn("Single Rider", r)
        self.assertNotIn("Indiana Jones", r)

    def test_ranking_sem_argumento_compara_todos_os_parques(self):
        r = self.cmd("/ranking")
        self.assertIn("Maiores filas agora", r)
        self.assertIn("Disney Hollywood Studios", r)
        self.assertEqual(len(self.requests.gets), len(self.parques))

    def test_ranking_hoje_usa_historico(self):
        parque = "Disney Hollywood Studios"
        agora_utc = dt.datetime(2026, 10, 13, 16, 0)
        self.gravar(parque, "Slinky Dog Dash", 70, agora_utc)
        self.gravar(parque, "Slinky Dog Dash", 50, agora_utc + dt.timedelta(minutes=5))
        self.gravar(parque, "Toy Story Mania!", 30, agora_utc)
        r = self.cmd("/ranking hoje")
        self.assertLess(r.index("Slinky Dog Dash"), r.index("Toy Story Mania!"))
        self.assertIn("média 60 min", r)
        self.assertEqual(self.requests.gets, [], "histórico não deve chamar a API")

    def test_ranking_explica_que_nao_mede_visitantes(self):
        self.assertIn("não por número de visitantes", self.cmd("/ranking Hollywood"))


class TestAutorizacao(BaseComando):
    def _update(self, chat_id, texto, update_id=1):
        return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": texto}}

    def test_so_o_chat_configurado_e_atendido(self):
        self.requests.roteador = lambda url: Resposta({"result": [
            self._update(int(CHAT_FAKE), "/parques", 10),
            self._update(999999, "/parques", 11),
        ]})
        offset = self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0)
        self.assertEqual(offset, 12, "offset avança mesmo para update recusado")
        self.assertEqual(len(self.enviadas()), 2)
        self.assertIn("Acesso restrito", self.enviadas()[1])

    def test_is_authorized_compara_como_texto(self):
        self.assertTrue(self.notifier.is_authorized(int(CHAT_FAKE)))
        self.assertTrue(self.notifier.is_authorized(CHAT_FAKE))
        self.assertFalse(self.notifier.is_authorized(999999))

    def test_senha_familiar_libera_chat_e_responde_ao_destinatario(self):
        self.monitor.FAMILY_ACCESS_PASSWORD = "senha-longa-de-teste"
        self.requests.roteador = lambda url: Resposta({"result": [
            self._update(999999, "/entrar senha-longa-de-teste", 20),
            self._update(999999, "/parques", 21),
        ]})
        self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0)
        self.assertTrue(self.monitor.chat_autorizado(self.conn, 999999))
        self.assertIn("Acesso familiar liberado", self.enviadas()[0])
        self.assertIn("Parques monitorados", self.enviadas()[1])
        self.assertTrue(all(str(p["chat_id"]) == "999999" for p in self.requests.posts))

    def test_senha_errada_nao_libera_chat(self):
        self.monitor.FAMILY_ACCESS_PASSWORD = "senha-longa-de-teste"
        resposta = self.monitor.autenticar_familiar(self.conn, 999999, "errada")
        self.assertIn("incorreta", resposta)
        self.assertFalse(self.monitor.chat_autorizado(self.conn, 999999))

    def test_update_sem_texto_e_ignorado(self):
        self.requests.roteador = lambda url: Resposta({"result": [
            {"update_id": 5, "message": {"chat": {"id": int(CHAT_FAKE)}}},
        ]})
        self.monitor.serve_commands(None, self.conn, self.config, self.parques, 0)
        self.assertEqual(self.enviadas(), [])


class TestNotifier(BaseComando):
    def test_format_alert_escapa_e_mostra_tendencia(self):
        texto = self.notifier.format_alert(
            "Epcot", "Mickey & Minnie's Runaway Railway", 30, 40, " ↓12")
        self.assertIn("Mickey &amp; Minnie", texto)
        self.assertIn("↓12", texto)

    def test_token_nao_aparece_no_texto_enviado(self):
        self.notifier.send("oi")
        for enviado in self.enviadas():
            self.assertNotIn("TOKEN-DE-TESTE", enviado)


if __name__ == "__main__":
    unittest.main()
