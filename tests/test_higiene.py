"""Itens B1-B7 da auditoria: higiene que não muda o que o usuário vê.

Cada teste aqui existe porque o item correspondente era invisível — nenhum
quebrava nada hoje, e é exatamente por isso que ficariam para sempre.
"""
import pathlib
import re
import sys
import unittest
from unittest.mock import patch

from tests.apoio import BaseTeste, Resposta

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.modules.setdefault("requests", __import__("tests.apoio", fromlist=["_requests"])._requests)
import api_server  # noqa: E402


class TestB1SemRaiseDuplicado(BaseTeste):
    """O 4xx não retentado é decisão do except, não de um raise extra."""

    def test_4xx_nao_e_retentado(self):
        self.requests.roteador = lambda url: Resposta({}, status=404)
        with self.assertRaises(Exception):
            self.monitor.get_json("https://exemplo/x")
        self.assertEqual(len(self.requests.gets), 1, "404 não melhora repetindo")

    def test_5xx_e_retentado_ate_o_limite(self):
        self.requests.roteador = lambda url: Resposta({}, status=503)
        with self.assertRaises(Exception):
            self.monitor.get_json("https://exemplo/x")
        self.assertEqual(len(self.requests.gets), self.monitor.HTTP_TENTATIVAS)

    def test_429_e_retentado(self):
        self.requests.roteador = lambda url: Resposta({}, status=429)
        with self.assertRaises(Exception):
            self.monitor.get_json("https://exemplo/x")
        self.assertEqual(len(self.requests.gets), self.monitor.HTTP_TENTATIVAS)


class TestB2CargaUnicaDoModulo(unittest.TestCase):
    """`python monitor.py` carregava o módulo duas vezes, com globais separadas.

    Não dá para testar rodando de verdade — `main()` é um laço infinito. O que dá
    para garantir é que o ponto de entrada delega para o módulo importado, em vez
    de chamar o `main` da cópia `__main__`.
    """

    def test_entrada_delega_para_o_modulo_importado(self):
        fim = (RAIZ / "monitor.py").read_text().split('if __name__ == "__main__":')[-1]
        self.assertIn("import monitor", fim)
        self.assertIn("monitor.main()", fim)
        self.assertNotRegex(fim, r"^\s{4}main\(\)\s*$",
                            "chamar main() direto usa a cópia __main__")


class TestB4SemGpsNoLog(unittest.TestCase):
    """O log do Docker guarda 30 MB e é legível por quem tiver o servidor."""

    def test_query_sai_da_linha_de_log(self):
        linha = '"GET /perto?lat=28.4123&lon=-81.5678 HTTP/1.1" 200 -'
        self.assertEqual(api_server._SEM_QUERY.sub("", linha),
                         '"GET /perto HTTP/1.1" 200 -')

    def test_rota_sem_query_fica_intacta(self):
        linha = '"GET /health HTTP/1.1" 200 -'
        self.assertEqual(api_server._SEM_QUERY.sub("", linha), linha)

    def test_nenhuma_coordenada_sobrevive(self):
        for linha in ('"GET /perto?lat=1.5&lon=-2.5 HTTP/1.1" 400 -',
                      '"GET /perto?lon=-81.5&lat=28.4&extra=x HTTP/1.1" 401 -'):
            with self.subTest(linha=linha):
                limpo = api_server._SEM_QUERY.sub("", linha)
                self.assertNotIn("lat", limpo)
                self.assertNotIn("lon", limpo)


class TestB5AvisoDeDisco(BaseTeste):
    def test_avisa_quando_o_disco_aperta(self):
        with patch.object(self.monitor, "espaco_em_disco", return_value=(1.2, 96.0)):
            self.monitor.avisar_disco_apertado()
        texto = self.enviadas()[0]
        self.assertIn("Disco do VPS apertando", texto)
        self.assertIn("1.2 GB", texto)
        self.assertIn("builder prune", texto, "o aviso tem que dizer o que fazer")

    def test_nao_avisa_com_disco_folgado(self):
        with patch.object(self.monitor, "espaco_em_disco", return_value=(20.0, 30.0)):
            self.monitor.avisar_disco_apertado()
        self.assertEqual(self.enviadas(), [])

    def test_disco_indisponivel_nao_avisa_nem_quebra(self):
        with patch.object(self.monitor, "espaco_em_disco", return_value=None):
            self.monitor.avisar_disco_apertado()
        self.assertEqual(self.enviadas(), [])

    def test_a_manutencao_diaria_carrega_o_aviso(self):
        """Herda o 'uma vez por dia' de graça, sem tabela nova."""
        with patch.object(self.monitor, "espaco_em_disco", return_value=(0.5, 99.0)):
            self.monitor.maybe_maintain_db(self.conn, self.config)
            self.monitor.maybe_maintain_db(self.conn, self.config)
        self.assertEqual(len([t for t in self.enviadas() if "Disco" in t]), 1)


class TestB5LimitesNoCompose(unittest.TestCase):
    def test_os_dois_servicos_tem_teto(self):
        compose = (RAIZ / "docker-compose.yml").read_text()
        self.assertEqual(compose.count("mem_limit:"), 2)
        self.assertEqual(compose.count("cpus:"), 2)


class TestB6Dependencias(unittest.TestCase):
    def test_toda_dependencia_tem_versao_e_hash(self):
        """Hash em uma só já obriga todas — meia lista quebra o build."""
        linhas = [l for l in (RAIZ / "requirements.txt").read_text().splitlines()
                  if l.strip() and not l.strip().startswith("#")]
        pacotes = [l for l in linhas if not l.strip().startswith("--hash")]
        self.assertTrue(pacotes)
        for pacote in pacotes:
            with self.subTest(pacote=pacote):
                self.assertIn("==", pacote, "versão solta reabre o rebuild surpresa")
        for nome in ("requests", "certifi", "charset-normalizer", "idna", "urllib3"):
            self.assertIn(nome, "\n".join(pacotes), f"{nome} sem hash quebra o pip")
        self.assertEqual(len(re.findall(r"--hash=sha256:[0-9a-f]{64}",
                                        (RAIZ / "requirements.txt").read_text())),
                         len(pacotes))

    def test_actions_fixadas_por_sha(self):
        """Tag é ponteiro móvel: `@v4` pode virar outro código sem diff aqui."""
        ci = (RAIZ / ".github" / "workflows" / "ci.yml").read_text()
        usos = re.findall(r"uses:\s*(\S+)", ci)
        self.assertTrue(usos)
        for uso in usos:
            with self.subTest(uso=uso):
                acao, _, referencia = uso.partition("@")
                self.assertRegex(referencia, r"^[0-9a-f]{40}$",
                                 f"{acao} está por tag, não por SHA")

    def test_cada_sha_diz_qual_versao_representa(self):
        """SHA sem comentário é ilegível: ninguém sabe se está velho."""
        ci = (RAIZ / ".github" / "workflows" / "ci.yml").read_text()
        for linha in ci.splitlines():
            if "uses:" in linha and "@" in linha:
                with self.subTest(linha=linha.strip()):
                    self.assertRegex(linha, r"#\s*v\d")

    def test_dependabot_cobre_pip_e_actions(self):
        cfg = (RAIZ / ".github" / "dependabot.yml").read_text()
        self.assertIn('package-ecosystem: "pip"', cfg)
        self.assertIn('package-ecosystem: "github-actions"', cfg)


class TestB7MetadadosDoRepo(unittest.TestCase):
    def test_licenca_e_politica_de_seguranca_existem(self):
        for nome in ("LICENSE", "SECURITY.md"):
            with self.subTest(arquivo=nome):
                self.assertTrue((RAIZ / nome).is_file())

    def test_security_md_diz_onde_reportar_em_privado(self):
        texto = (RAIZ / "SECURITY.md").read_text()
        self.assertIn("security/advisories/new", texto)
        self.assertIn("Não abra issue pública", texto)


if __name__ == "__main__":
    unittest.main()
