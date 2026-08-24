"""Aviso de atração nova e o /novidades.

O filtro de watchlist existe para a mensagem não virar lista de 76 itens, e
está certo. O que faltava era ele DIZER o que descartou: uma atração que abrir
em setembro ficava invisível no alerta, no /status e no ranking, sem nenhum
sinal — o usuário só descobriria chegando no parque.
"""
import unittest

from tests.apoio import BaseTeste


def payload(*nomes_e_filas):
    return {"lands": [{"name": "Terra", "rides": [
        {"id": i, "name": nome, "is_open": True, "wait_time": fila}
        for i, (nome, fila) in enumerate(nomes_e_filas)]}]}


PARQUE = "Disney Magic Kingdom"
CONHECIDA = "Space Mountain"          # está na watchlist
FORA = "Astro Orbiter"                # existe no parque, fora da watchlist


class TestPrimeiraLeitura(BaseTeste):
    def test_a_primeira_leitura_nao_avisa_nada(self):
        """Sem semeadura, o primeiro ciclo anunciaria o parque inteiro.

        São 76 atrações no Magic Kingdom. O aviso que existe para chamar
        atenção viraria a coisa que se aprende a ignorar.
        """
        novas = self.monitor.registrar_atracoes(
            self.conn, PARQUE, payload((CONHECIDA, 30), (FORA, 5)))
        self.assertEqual(novas, [])
        self.assertEqual(self.monitor.atracoes_a_avisar(self.conn, PARQUE), [])

    def test_o_que_aparece_depois_e_novidade(self):
        self.monitor.registrar_atracoes(self.conn, PARQUE, payload((CONHECIDA, 30)))
        novas = self.monitor.registrar_atracoes(
            self.conn, PARQUE, payload((CONHECIDA, 30), ("Brinquedo Novo", 45)))
        self.assertEqual(novas, ["Brinquedo Novo"])

    def test_atracao_que_some_e_volta_nao_e_novidade_de_novo(self):
        """Manutenção tira a atração do feed; voltar não é abrir."""
        self.monitor.registrar_atracoes(self.conn, PARQUE, payload((CONHECIDA, 30)))
        self.monitor.registrar_atracoes(self.conn, PARQUE, payload(("Outra", 10)))
        self.monitor.marcar_avisadas(self.conn, PARQUE, ["Outra"])
        novas = self.monitor.registrar_atracoes(
            self.conn, PARQUE, payload((CONHECIDA, 30), ("Outra", 10)))
        self.assertEqual(novas, [])

    def test_fila_paralela_nunca_e_novidade(self):
        """Regra 10: single rider é atração separada na API, e nunca interessa."""
        self.monitor.registrar_atracoes(self.conn, PARQUE, payload((CONHECIDA, 30)))
        novas = self.monitor.registrar_atracoes(
            self.conn, PARQUE,
            payload((CONHECIDA, 30), ("Space Mountain Single Rider", 0)))
        self.assertEqual(novas, [])

    def test_parques_sao_independentes(self):
        """Semear o Magic Kingdom não pode calar o Epcot."""
        self.monitor.registrar_atracoes(self.conn, PARQUE, payload((CONHECIDA, 30)))
        novas = self.monitor.registrar_atracoes(self.conn, "Epcot", payload(("Test Track", 30)))
        self.assertEqual(novas, [])  # primeira leitura do Epcot também semeia


class TestAviso(BaseTeste):
    def _semear(self):
        self.monitor.registrar_atracoes(self.conn, PARQUE, payload((CONHECIDA, 30)))

    def test_avisa_uma_vez_e_so_uma(self):
        self._semear()
        novo = payload((CONHECIDA, 30), ("Brinquedo Novo", 45))
        self.monitor.avisar_atracoes_novas(self.conn, self.config, PARQUE, novo)
        self.monitor.avisar_atracoes_novas(self.conn, self.config, PARQUE, novo)
        avisos = [t for t in self.enviadas() if "Atração nova" in t]
        self.assertEqual(len(avisos), 1, "o segundo ciclo não pode repetir")
        self.assertIn("Brinquedo Novo", avisos[0])
        self.assertIn("45 min", avisos[0])

    def test_falha_de_envio_deixa_para_o_proximo_ciclo(self):
        self._semear()
        novo = payload((CONHECIDA, 30), ("Brinquedo Novo", 45))
        # O Telegram usa POST: injetar no roteador de GET não derruba nada.
        self.requests.roteador_post = lambda url, corpo: (_ for _ in ()).throw(
            self.requests.RequestException("Telegram fora"))
        self.monitor.avisar_atracoes_novas(self.conn, self.config, PARQUE, novo)
        self.assertEqual(self.monitor.atracoes_a_avisar(self.conn, PARQUE),
                         ["Brinquedo Novo"], "não pode marcar como avisada")

    def test_sem_fila_publicada_nao_vira_zero(self):
        """Regra 15: ausência de dado nunca vira 0 min."""
        self._semear()
        self.monitor.avisar_atracoes_novas(
            self.conn, self.config, PARQUE,
            payload((CONHECIDA, 30), ("Brinquedo Novo", None)))
        aviso = [t for t in self.enviadas() if "Atração nova" in t][0]
        self.assertIn("sem fila publicada", aviso)
        self.assertNotIn("0 min", aviso)

    def test_o_e_comercial_e_escapado(self):
        """Regra 8: nome cru com & derruba a mensagem com 400 do Telegram."""
        texto = self.monitor.format_atracao_nova(PARQUE, "Mickey & Minnie's Railway", 20)
        self.assertIn("&amp;", texto)
        self.assertNotIn("& M", texto)

    def test_atracao_adicionada_a_watchlist_antes_do_aviso_nao_avisa(self):
        """Se ela entrou na watchlist no meio, o aviso perdeu o sentido."""
        self._semear()
        self.monitor.registrar_atracoes(
            self.conn, PARQUE, payload((CONHECIDA, 30), ("Jungle Cruise", 40)))
        self.monitor.avisar_atracoes_novas(
            self.conn, self.config, PARQUE,
            payload((CONHECIDA, 30), ("Jungle Cruise", 40)))
        self.assertEqual([t for t in self.enviadas() if "Atração nova" in t], [])
        self.assertEqual(self.monitor.atracoes_a_avisar(self.conn, PARQUE), [])


class TestNovidadesComando(BaseTeste):
    def test_lista_o_que_esta_fora_da_watchlist(self):
        texto = self.monitor.format_novidades(
            self.config, PARQUE, payload((CONHECIDA, 30), (FORA, 15)))
        self.assertIn(FORA, texto)
        self.assertNotIn(CONHECIDA, texto)

    def test_ordena_por_fila_e_separa_quem_nao_tem(self):
        texto = self.monitor.format_novidades(
            self.config, PARQUE,
            payload(("Sem Fila", None), ("Fila Curta", 5), ("Fila Longa", 60)))
        self.assertLess(texto.index("Fila Longa"), texto.index("Fila Curta"))
        self.assertIn("Sem fila publicada agora", texto)

    def test_watchlist_completa_diz_isso(self):
        texto = self.monitor.format_novidades(self.config, PARQUE, payload((CONHECIDA, 30)))
        self.assertIn("está na watchlist", texto)

    def test_fila_paralela_nao_aparece(self):
        texto = self.monitor.format_novidades(
            self.config, PARQUE, payload((CONHECIDA, 30), ("Space Mountain Single Rider", 0)))
        self.assertNotIn("Single Rider", texto)

    def test_atracao_fechada_nao_e_listada_como_fila_de_zero(self):
        """Visto no site em 24/08: o /novidades do Animal Kingdom listava as 14
        atrações como "— 0 min", brinquedo parado e show no mesmo plano de uma
        fila curta de verdade. Fechada agora tem seção própria e sem número."""
        p = {"lands": [{"name": "Terra", "rides": [
            {"id": 1, "name": "Astro Orbiter", "is_open": False, "wait_time": 0},
            {"id": 2, "name": "Mad Tea Party", "is_open": True, "wait_time": 40},
        ]}]}
        texto = self.monitor.format_novidades(self.config, PARQUE, p, self.conn)
        self.assertIn("Fechadas agora", texto)
        self.assertIn("Mad Tea Party — 40 min", texto)
        self.assertNotIn("Astro Orbiter — 0 min", texto)

    def test_show_com_historico_de_zero_vai_para_secao_propria(self):
        """Quem separa show de brinquedo parado é o histórico, não o nome."""
        import datetime as dt
        base = dt.datetime(2026, 10, 6, 18)
        for leitura in range(self.monitor.MIN_LEITURAS_PLACEHOLDER):
            self.gravar(PARQUE, "Tree of Life", 0,
                        base + dt.timedelta(minutes=5 * leitura))
        p = {"lands": [{"name": "Terra", "rides": [
            {"id": 1, "name": "Tree of Life", "is_open": True, "wait_time": 0},
        ]}]}
        texto = self.monitor.format_novidades(self.config, PARQUE, p, self.conn)
        self.assertIn("Shows e sem fila medida", texto)
        self.assertNotIn("Tree of Life — 0 min", texto)

    def test_sem_conn_o_comando_ainda_responde(self):
        """O detector precisa do banco; sem ele o comando perde o terceiro
        grupo, mas não pode quebrar."""
        texto = self.monitor.format_novidades(
            self.config, PARQUE, payload((FORA, 15)))
        self.assertIn(FORA, texto)

    def test_conta_todas_as_secoes_no_total(self):
        p = {"lands": [{"name": "Terra", "rides": [
            {"id": 1, "name": "Astro Orbiter", "is_open": False, "wait_time": 0},
            {"id": 2, "name": "Mad Tea Party", "is_open": True, "wait_time": 40},
            {"id": 3, "name": "Tiki Room", "is_open": True, "wait_time": None},
        ]}]}
        texto = self.monitor.format_novidades(self.config, PARQUE, p, self.conn)
        self.assertIn("3 atração(ões)", texto)

    def test_esta_no_help(self):
        self.assertIn("/novidades", self.monitor.HELP)
