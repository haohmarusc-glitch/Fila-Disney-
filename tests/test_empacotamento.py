"""O Dockerfile leva todos os módulos que o código importa?

Existe porque o localizacao.py foi criado e não entrou no COPY do Dockerfile:
o build passou (docker build não importa nada), o CI ficou verde, e o container
entrou em loop de reinício com ModuleNotFoundError já em produção.
"""
import ast
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def modulos_locais() -> set[str]:
    return {p.stem for p in RAIZ.glob("*.py")}


def importados_por(arquivo: Path, locais: set[str]) -> set[str]:
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
    usados = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            usados |= {a.name.split(".")[0] for a in no.names}
        elif isinstance(no, ast.ImportFrom) and no.module and no.level == 0:
            usados.add(no.module.split(".")[0])
    return usados & locais


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (RAIZ / "Dockerfile").read_text(encoding="utf-8")
        self.copiados = set(re.findall(r"([\w./\[\]-]+\.py)", self.dockerfile))
        self.locais = modulos_locais()

    def test_tudo_que_o_monitor_importa_vai_para_a_imagem(self):
        faltando = {
            f"{m}.py" for m in importados_por(RAIZ / "monitor.py", self.locais)
        } - self.copiados
        self.assertFalse(faltando, f"módulo importado mas fora do COPY: {faltando}")

    def test_dependencia_transitiva_tambem_vai(self):
        """localizacao importa monitor e notifier; coords importa os dois."""
        for origem in ("localizacao.py", "coords.py", "healthcheck.py", "analyze.py"):
            caminho = RAIZ / origem
            if not caminho.exists():
                continue
            faltando = {
                f"{m}.py" for m in importados_por(caminho, self.locais)
            } - self.copiados
            self.assertFalse(faltando, f"{origem} importa mas não está no COPY: {faltando}")

    def test_todo_modulo_do_projeto_esta_no_copy(self):
        """Regra simples: se é .py na raiz, vai para a imagem."""
        faltando = {f"{m}.py" for m in self.locais} - self.copiados
        self.assertFalse(faltando, f"módulo do projeto fora do Dockerfile: {faltando}")


if __name__ == "__main__":
    unittest.main()
