"""Enriquecimento de durações — NÃO é etapa obrigatória do sistema.

O banco de durações é o duracoes.json, versionado no repositório. O bot lê só
ele; a Wikipédia existe para PREENCHER esse arquivo, não para servi-lo. Se a
Wikipédia estiver fora, o que já está no duracoes.json continua valendo — só não
ganha duração nova. Atração sem entrada aparece sem duração, nunca com
estimativa (regra 12).

A Queue-Times não publica duração: entrega `id`, `name`, `is_open`, `wait_time`
e `last_updated`, e nada mais. A themeparks.wiki também não — conferido em
24/08/2026, o schema de entidade tem só entityType, externalId, id, location,
name, parentId e slug. Sobrou a Wikipédia, cujo infobox de atração traz
`duration`.

Uso:
    docker compose exec fila-disney python duracoes.py --revisar   # só relatório
    docker compose exec fila-disney python duracoes.py             # grava
    docker compose exec fila-disney python duracoes.py --sobrescrever

Cada duração encontrada vem com a página de origem no relatório, para dar para
conferir na mão antes de aceitar. O script nunca inventa: atração cuja página
não tenha `duration` no infobox fica de fora e é listada no fim.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import monitor

WIKI_API = "https://en.wikipedia.org/w/api.php"
# A Wikipédia pede uso moderado e User-Agent identificável. São ~54 atrações,
# duas chamadas cada: com meio segundo entre elas a execução leva ~1 min e não
# encosta em limite nenhum.
PAUSA_ENTRE_CHAMADAS_S = 0.5
DURACOES_PATH = monitor.DURACOES_PATH

# Formatos que aparecem de verdade no infobox: "3 minutes", "2:30",
# "1 minute, 30 seconds", "4 minutes 30 seconds", "90 seconds".
_MIN_SEG = re.compile(r"(\d+)\s*(?:minutes?|min)\b[^0-9]{0,12}?(\d+)\s*(?:seconds?|sec)\b", re.I)
_MM_SS = re.compile(r"\b(\d{1,3}):([0-5]\d)\b")
_SO_MIN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:minutes?|min)\b", re.I)
_SO_SEG = re.compile(r"(\d+)\s*(?:seconds?|sec)\b", re.I)


def minutos_do_texto(texto: str) -> int | None:
    """Converte o campo `duration` do infobox em minutos inteiros.

    Devolve None quando não reconhece, e isso é melhor que chutar: atração
    sem duração aparece sem duração, nunca com estimativa (regra 12).
    """
    if not texto:
        return None
    # `{{convert|3|min}}` e `{{nowrap|2:30}}` são comuns no infobox. Trocar
    # chave e barra por espaço deixa os números legíveis pelos padrões abaixo.
    texto = re.sub(r"[{}|]", " ", texto)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = texto.replace("[[", " ").replace("]]", " ").replace("'", " ")
    casado = _MIN_SEG.search(texto)
    if casado:
        segundos = int(casado.group(1)) * 60 + int(casado.group(2))
    elif _MM_SS.search(texto):
        m, s = _MM_SS.search(texto).groups()
        segundos = int(m) * 60 + int(s)
    elif _SO_MIN.search(texto):
        segundos = float(_SO_MIN.search(texto).group(1)) * 60
    elif _SO_SEG.search(texto):
        segundos = int(_SO_SEG.search(texto).group(1))
    else:
        return None
    if segundos <= 0:
        return None
    # Meio a meio arredonda para CIMA. O round() do Python usa arredondamento
    # bancário: round(2.5) devolve 2, e "2:30" viraria 2 min. Subestimar o
    # compromisso de tempo é o erro que atrapalha quem está decidindo se cabe
    # antes de fechar.
    return max(1, int(segundos / 60 + 0.5))


# Atração que existe em vários resorts tem UM artigo na Wikipédia cobrindo
# todos. O infobox numera as instalações — `park2 = Magic Kingdom` vem com
# `duration2` — então dá para pegar a duração DO NOSSO parque em vez de recusar
# o artigo inteiro. Contar parques e desistir descartava clone de duração
# idêntica (Rise of the Resistance, Rock 'n' Roller Coaster, Seven Dwarfs Mine
# Train) junto com o caso legítimo do Pirates, que tem 16 min na Disneyland e
# bem menos no Magic Kingdom.
#
# Os nomes da watchlist não são os da Wikipédia: aqui é "Disney Hollywood
# Studios", lá é "Disney's Hollywood Studios"; aqui "Universal Studios At
# Universal Orlando", lá "Universal Studios Florida".
PARQUES_WIKI = {
    "Disney Magic Kingdom": ("magic kingdom",),
    "Epcot": ("epcot",),
    "Disney Hollywood Studios": ("hollywood studios",),
    "Disney Animal Kingdom": ("animal kingdom",),
    "Universal Studios At Universal Orlando": ("universal studios florida",),
    "Islands Of Adventure At Universal Orlando": ("islands of adventure",),
    "Universal Epic Universe": ("epic universe",),
}


# Rede de segurança independente do parser. Se o infobox cita mais de um destes,
# a duração solta não diz de qual instalação ela é — e o parser pode ter falhado
# em enxergar as chaves `park2`, `park3` que separariam.
RESORTS = (
    "disneyland park", "disneyland resort", "magic kingdom", "tokyo disneyland",
    "tokyo disneysea", "disneyland paris", "walt disney studios park",
    "shanghai disneyland", "hong kong disneyland", "disney california adventure",
    "epcot", "hollywood studios", "animal kingdom",
    "universal studios hollywood", "universal studios florida",
    "universal studios japan", "universal studios singapore",
    "universal studios beijing", "islands of adventure", "epic universe",
)


def resorts_citados(wikitexto: str) -> set[str]:
    """Resorts nomeados na região do infobox."""
    texto = wikitexto.lower()
    fim = texto.find("\n}}")
    return {nome for nome in RESORTS if nome in texto[:fim if fim > 0 else 4000]}


def parametros_do_infobox(wikitexto: str) -> dict[str, str]:
    """{chave: valor} do primeiro infobox. Chaves em minúsculo, sem espaço."""
    # Busca sem caso: a Wikipédia aceita {{infobox}} e {{Infobox}}, e o
    # find() sensível a maiúscula devolvia {} para os artigos em minúscula.
    inicio = wikitexto.lower().find("{{infobox")
    if inicio < 0:
        return {}
    profundidade, fim = 0, len(wikitexto)
    for i in range(inicio, len(wikitexto) - 1):
        if wikitexto[i:i + 2] == "{{":
            profundidade += 1
        elif wikitexto[i:i + 2] == "}}":
            profundidade -= 1
            if profundidade == 0:
                fim = i
                break
    corpo = wikitexto[inicio:fim]
    saida = {}
    # A quebra de linha antes do | separa parametro de topo; o | dentro de
    # {{convert|...}} fica na mesma linha e nao vira chave falsa.
    for pedaco in re.split(r"\n\s*\|", corpo)[1:]:
        if "=" not in pedaco:
            continue
        chave, _, valor = pedaco.partition("=")
        saida[chave.strip().lower()] = valor.strip()
    return saida


def duracao_para_o_parque(wikitexto: str, parque: str) -> int | None:
    """Duração da instalação DESTE parque, ou levanta Ambigua."""
    params = parametros_do_infobox(wikitexto)
    instalacoes = {m.group(1): valor for chave, valor in params.items()
                   if (m := re.fullmatch(r"park(\d*)", chave)) and valor}
    if len(instalacoes) <= 1:
        # "Uma instalação" pode significar duas coisas: o artigo é de um parque
        # só, ou o parser não enxergou as chaves que separam os outros. O
        # segundo caso trouxe o Pirates de volta com 16 min — o número da
        # Disneyland — quando esta função passou a aceitar a duração solta.
        # Por isso a checagem por NOME é rede independente do parser.
        if len(resorts_citados(wikitexto)) > 1:
            raise Ambigua(sorted(resorts_citados(wikitexto)))
        return (minutos_do_texto(params.get("duration", ""))
                or minutos_do_texto(campo_duration(wikitexto) or ""))

    esperados = PARQUES_WIKI.get(parque, ())
    for indice, nome in instalacoes.items():
        nome = nome.lower()
        if not any(alvo in nome for alvo in esperados):
            continue
        propria = params.get(f"duration{indice}")
        if propria:
            return minutos_do_texto(propria)
        # Parque achado, mas sem duração própria: a duração solta e do artigo
        # inteiro e pode ser de outra instalacao. E o caso do Pirates.
        break
    raise Ambigua(sorted(v.lower() for v in instalacoes.values()))


def campo_duration(wikitexto: str) -> str | None:
    """Extrai o valor de `duration` do infobox, parando na próxima chave."""
    # O fim do valor é a próxima chave, o fecho do template — ou o fim do
    # texto, que faltava e fazia o campo sumir quando era o último do infobox.
    casado = re.search(r"\|\s*duration\s*=\s*(.+?)(?=\n\s*\||\n\}\}|\Z)",
                       wikitexto, re.S | re.I)
    return casado.group(1).strip() if casado else None


class Ambigua(Exception):
    """O artigo cobre mais de um resort: a duração do infobox não diz qual."""


def consultar(parametros: dict) -> dict:
    """GET na API da Wikipédia pelo `get_json` do monitor (regra 11).

    A query vai montada na URL porque `get_json(url, *, tentativas)` não recebe
    `params` — chamá-lo com esse argumento levantava TypeError em toda atração,
    e o `except` largo abaixo rotulava isso como "falha de rede". Erro de
    programação disfarçado de problema externo é o pior tipo de log.
    """
    return monitor.get_json(f"{WIKI_API}?{urlencode(parametros)}")


def buscar_pagina(nome: str) -> str | None:
    """Título da página mais provável para esta atração, ou None.

    O título tem que compartilhar o nome da atração depois de normalizado — sem
    isso a busca devolveria o artigo do filme, do personagem ou de uma atração
    homônima em outro parque, e a duração entraria errada sem ninguém notar.
    """
    dados = consultar({"action": "query", "list": "search", "srlimit": 5,
                       "srsearch": f"{nome} attraction", "format": "json"})
    alvo = monitor.normalizar_nome_api(nome)
    for item in dados.get("query", {}).get("search", []):
        titulo = monitor.normalizar_nome_api(item["title"])
        if titulo in alvo or alvo in titulo:
            return item["title"]
    return None


def duracao_da_pagina(titulo: str, parque: str) -> int | None:
    dados = consultar({"action": "query", "prop": "revisions", "rvprop": "content",
                       "rvslots": "main", "titles": titulo, "format": "json"})
    for pagina in dados.get("query", {}).get("pages", {}).values():
        try:
            texto = pagina["revisions"][0]["slots"]["main"]["*"]
        except (KeyError, IndexError, TypeError):
            continue
        return duracao_para_o_parque(texto, parque)
    return None


def carregar_arquivo() -> dict:
    try:
        with open(DURACOES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"rides": {}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revisar", action="store_true", help="só relatório, não grava")
    ap.add_argument("--sobrescrever", action="store_true",
                    help="substitui durações já preenchidas")
    args = ap.parse_args()

    config = monitor.load_config()
    dados = carregar_arquivo()
    dados.setdefault("rides", {})
    achadas, faltando = 0, []

    for parque, cfg in config["parks"].items():
        atuais = dados["rides"].setdefault(parque, {})
        print(f"\n{parque}")
        for atracao in cfg.get("attractions", {}):
            if atracao in atuais and not args.sobrescrever:
                print(f"  ✓ {atracao} — já tem {atuais[atracao]} min")
                continue
            try:
                titulo = buscar_pagina(atracao)
                time.sleep(PAUSA_ENTRE_CHAMADAS_S)
                minutos = duracao_da_pagina(titulo, parque) if titulo else None
                time.sleep(PAUSA_ENTRE_CHAMADAS_S)
            except Ambigua as exc:
                onde = ", ".join(exc.args[0])
                print(f"  ? {atracao} — artigo cobre vários parques ({onde})")
                faltando.append((parque, atracao, f"ambíguo: {onde}"))
                continue
            except Exception as exc:  # noqa: BLE001 — uma atração não derruba o resto
                motivo = (f"falha de rede ({type(exc).__name__})"
                          if isinstance(exc, (OSError, ValueError))
                          or "requests" in type(exc).__module__
                          else f"ERRO NO SCRIPT: {type(exc).__name__}: {exc}")
                print(f"  ! {atracao} — {motivo}")
                faltando.append((parque, atracao, motivo))
                continue
            if minutos is None:
                motivo = "sem infobox duration" if titulo else "página não encontrada"
                print(f"  – {atracao} — {motivo}")
                faltando.append((parque, atracao, motivo))
                continue
            atuais[atracao] = minutos
            achadas += 1
            print(f"  + {atracao} — {minutos} min (via '{titulo}')")

    print(f"\n{achadas} duração(ões) nova(s); {len(faltando)} sem dado.")
    if faltando:
        print("Ficam SEM duração na tela, que é o comportamento correto:")
        for parque, atracao, motivo in faltando:
            print(f"  {parque} / {atracao} — {motivo}")

    if args.revisar:
        print("\n--revisar: nada gravado.")
        return 0
    Path(DURACOES_PATH).write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nGravado em {DURACOES_PATH}")
    print("Este arquivo é versionado: rode `git diff duracoes.json` e confira "
          "antes de commitar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
