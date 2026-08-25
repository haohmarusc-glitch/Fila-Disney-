"""Troca (ou acrescenta) um bloco de site no Caddyfile, sem duplicar.

Existe porque o Caddyfile da VPS é do Premercado: dois blocos com o mesmo
hostname fazem o Caddy recusar a configuração inteira, e aí o premercadosc.com
cai junto com o site das filas. O runbook mandava `cat >>`, que é exatamente o
jeito de criar o segundo bloco na segunda vez que alguém roda.

Uso:
    python3 caddy_bloco.py Caddyfile filadisney.premercadosc.com bloco.txt
Escreve o resultado na saída padrão; não toca no arquivo original.
"""

import sys


def _sem_ruido(linha):
    """A linha com strings e comentário removidos, para contar chaves.

    `header_up Authorization "Bearer {env.WEB_API_TOKEN}"` tem chaves que
    equilibram sozinhas, mas um `# fecha aqui }` não — e um comentário assim
    faria o contador terminar o bloco no lugar errado.
    """
    fora = []
    aspas = None
    for ch in linha:
        if aspas:
            if ch == aspas:
                aspas = None
            continue
        if ch in ('"', "'", "`"):
            aspas = ch
            continue
        if ch == "#":
            break
        fora.append(ch)
    return "".join(fora)


def _abre_bloco(linha, hostname):
    """True se esta linha começa o bloco do hostname.

    Casa `filadisney.premercadosc.com {` e também a forma com vários endereços
    (`a.com, filadisney.premercadosc.com {`), mas nunca uma diretiva indentada
    lá dentro que por acaso cite o nome.
    """
    limpa = _sem_ruido(linha)
    if not limpa.rstrip().endswith("{"):
        return False
    if linha[:1].isspace():
        return False
    enderecos = limpa.rstrip().rstrip("{").strip()
    return any(p.strip() == hostname for p in enderecos.split(","))


def trocar(caddyfile, hostname, bloco):
    """Devolve o Caddyfile com o bloco do hostname substituído ou acrescentado."""
    linhas = caddyfile.splitlines()
    bloco = bloco.rstrip("\n")

    inicio = None
    for i, linha in enumerate(linhas):
        if _abre_bloco(linha, hostname):
            inicio = i
            break

    if inicio is None:
        corpo = "\n".join(linhas).rstrip("\n")
        return (corpo + "\n\n" + bloco + "\n") if corpo else bloco + "\n"

    profundidade = 0
    fim = None
    for i in range(inicio, len(linhas)):
        limpa = _sem_ruido(linhas[i])
        profundidade += limpa.count("{") - limpa.count("}")
        if profundidade == 0:
            fim = i
            break
    if fim is None:
        raise ValueError(
            f"bloco de {hostname} começa na linha {inicio + 1} e nunca fecha; "
            "não vou reescrever um Caddyfile que já está quebrado"
        )

    novas = linhas[:inicio] + bloco.split("\n") + linhas[fim + 1:]
    return "\n".join(novas).rstrip("\n") + "\n"


def main():
    if len(sys.argv) != 4:
        sys.exit("uso: caddy_bloco.py <Caddyfile> <hostname> <arquivo-do-bloco>")
    _, caminho, hostname, bloco_path = sys.argv
    with open(caminho, encoding="utf-8") as f:
        atual = f.read()
    with open(bloco_path, encoding="utf-8") as f:
        bloco = f.read()
    sys.stdout.write(trocar(atual, hostname, bloco))


if __name__ == "__main__":
    main()
