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
    docker compose exec fila-disney python duracoes.py --diagnostico  # infobox cru
    docker compose exec fila-disney python duracoes.py --wikidata     # P2047

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
# O Wikidata é a segunda fonte, e existe por um motivo que o infobox não
# resolve: lá cada INSTALAÇÃO é um item próprio. A Haunted Mansion do Magic
# Kingdom e a da Disneyland são Q diferentes, então a duração já vem sem a
# ambiguidade que barrou sete atrações aqui. O campo é o P2047.
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Só as unidades que aparecem de verdade. O relatório imprime o Q cru junto,
# então unidade nova aparece na tela em vez de virar conversão errada.
UNIDADES_WIKIDATA = {"Q7727": "min", "Q11574": "s", "Q25235": "h"}
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
_SO_NUMERO = re.compile(r"\s*(\d+(?:\.\d+)?)\s*")


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
    elif _SO_NUMERO.fullmatch(texto):
        # Campo `duration` com numero pelado e minuto por convencao do infobox
        # — o MEN IN BLACK traz "5.00" e nada mais. So vale para o campo
        # INTEIRO: numero solto no meio de frase pode ser altura, ano ou
        # capacidade, e virar duracao seria o palpite que a regra 12 proibe.
        segundos = float(_SO_NUMERO.fullmatch(texto).group(1)) * 60
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


# Busca por nome às vezes cai no artigo errado, e aí não há parser que salve:
# "TRON Lightcycle / Run" casa com o artigo do FILME Tron, e "Space Mountain"
# casa com o artigo genérico que cobre cinco parques em vez do dedicado do
# Magic Kingdom. São poucas e são estáveis — endereço de artigo, não número,
# então fixar aqui não esbarra na regra 12.
PAGINAS_WIKI = {
    ("Disney Magic Kingdom", "Space Mountain"): "Space Mountain (Magic Kingdom)",
    ("Disney Magic Kingdom", "TRON Lightcycle / Run"): "Tron Lightcycle Power Run",
    ("Universal Studios At Universal Orlando", "Villain-Con Minion Blast"):
        "Illumination's Villain-Con Minion Blast",
    ("Islands Of Adventure At Universal Orlando", "Jurassic Park River Adventure"):
        "Jurassic Park: The Ride",
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
                or minutos_do_texto(campo_duration(wikitexto) or "")
                or duracao_das_pistas(params))

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


def duracao_das_pistas(params: dict[str, str]) -> int | None:
    """`duration1`/`duration2` sem `park1`/`park2` são pistas, não parques.

    O Space Mountain do Magic Kingdom tem duas pistas, Alpha e Omega, e o
    infobox numera as durações sem numerar parque nenhum: `duration1 = 2:30` e
    `duration2 = 2:30`. Sem isto, o artigo CERTO devolvia None — a chave
    `duration` pelada não existe ali, e a numerada só era lida no caminho de
    artigo multiparque, que este não é.

    Só vale quando as numeradas concordam. Divergindo, não dá para saber qual
    o visitante vai pegar, e escolher uma seria estimativa (regra 12). Quem
    chama já garantiu que o artigo é de um parque só.
    """
    minutos = {minutos_do_texto(valor) for chave, valor in params.items()
               if re.fullmatch(r"duration\d+", chave) and valor}
    minutos.discard(None)
    return minutos.pop() if len(minutos) == 1 else None


def campo_duration(wikitexto: str) -> str | None:
    """Extrai o valor de `duration` do infobox, parando na próxima chave."""
    # O fim do valor é a próxima chave, o fecho do template — ou o fim do
    # texto, que faltava e fazia o campo sumir quando era o último do infobox.
    # O `(?!\s*\|)` e o que impede campo VAZIO de engolir o campo seguinte. Sem
    # ele, `| duration =` seguido de `| restriction_in = 52` capturava a linha
    # do restriction inteira — visto no Space Mountain, no Doctor Doom's
    # Fearfall e no Monsters Unchained. Nenhum virou numero errado por sorte
    # (nenhum trazia "minutes"), mas ler o campo errado e bug, nao estilo.
    casado = re.search(r"\|\s*duration\s*=[ \t]*(?!\s*\|)(.+?)(?=\n\s*\||\n\}\}|\Z)",
                       wikitexto, re.S | re.I)
    return casado.group(1).strip() if casado else None


class Ambigua(Exception):
    """O artigo cobre mais de um resort: a duração do infobox não diz qual."""


def consultar(parametros: dict, base: str = WIKI_API) -> dict:
    """GET na API da Wikipédia pelo `get_json` do monitor (regra 11).

    A query vai montada na URL porque `get_json(url, *, tentativas)` não recebe
    `params` — chamá-lo com esse argumento levantava TypeError em toda atração,
    e o `except` largo abaixo rotulava isso como "falha de rede". Erro de
    programação disfarçado de problema externo é o pior tipo de log.
    """
    return monitor.get_json(f"{base}?{urlencode(parametros)}")


def buscar_pagina(nome: str, parque: str | None = None) -> str | None:
    """Título da página mais provável para esta atração, ou None.

    O título tem que compartilhar o nome da atração depois de normalizado — sem
    isso a busca devolveria o artigo do filme, do personagem ou de uma atração
    homônima em outro parque, e a duração entraria errada sem ninguém notar.
    """
    if fixado := PAGINAS_WIKI.get((parque, nome)):
        return fixado
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


def campos_crus(titulo: str, parque: str) -> dict:
    """Campos do infobox que decidem a duração, sem interpretar nada.

    O parser é conservador de propósito com artigo que cobre vários parques:
    recusa em vez de arriscar o número da outra instalação — foi o Pirates,
    16 min na Disneyland contra bem menos no Magic Kingdom, que ensinou isso.
    Só que recusar joga fora junto o clone idêntico, onde a atração é a mesma
    nos dois parques e a duração única vale para o nosso.

    Distinguir os dois casos é leitura, não heurística. Este modo põe o texto
    cru na tela para alguém decidir; o número continua vindo do infobox, nunca
    de estimativa (regra 12).
    """
    dados = consultar({"action": "query", "prop": "revisions", "rvprop": "content",
                       "rvslots": "main", "titles": titulo, "format": "json"})
    for pagina in dados.get("query", {}).get("pages", {}).values():
        try:
            texto = pagina["revisions"][0]["slots"]["main"]["*"]
        except (KeyError, IndexError, TypeError):
            continue
        params = parametros_do_infobox(texto)
        duracoes = {c: v for c, v in params.items()
                    if re.fullmatch(r"duration\d*", c) and v}
        if not duracoes and (solto := campo_duration(texto)):
            duracoes["duration (fora do infobox)"] = solto
        return {
            "parques_do_infobox": {c: v for c, v in params.items()
                                   if re.fullmatch(r"park\d*", c) and v},
            "duracoes": duracoes,
            "resorts_citados": sorted(resorts_citados(texto)),
        }
    return {}


def relatar_campos_crus(config: dict, dados: dict) -> None:
    """Imprime o infobox cru de cada atração que ainda está sem duração."""
    for parque, cfg in config["parks"].items():
        atuais = dados["rides"].get(parque, {})
        pendentes = [a for a in cfg.get("attractions", {}) if a not in atuais]
        if not pendentes:
            continue
        print(f"\n=== {parque}")
        for atracao in pendentes:
            try:
                titulo = buscar_pagina(atracao, parque)
                time.sleep(PAUSA_ENTRE_CHAMADAS_S)
                campos = campos_crus(titulo, parque) if titulo else {}
                time.sleep(PAUSA_ENTRE_CHAMADAS_S)
            except Exception as exc:  # noqa: BLE001 — uma atração não derruba o resto
                print(f"  {atracao}\n     ! {type(exc).__name__}: {exc}")
                continue
            if not titulo:
                print(f"  {atracao}\n     página não encontrada")
                continue
            print(f"  {atracao}  [{titulo}]")
            if not campos.get("duracoes"):
                print("     sem duration no infobox")
            for chave, valor in campos.get("duracoes", {}).items():
                print(f"     {chave} = {valor}")
            for chave, valor in campos.get("parques_do_infobox", {}).items():
                print(f"     {chave} = {valor}")
            citados = campos.get("resorts_citados", [])
            if len(citados) > 1:
                print(f"     resorts citados: {', '.join(citados)}")


def buscar_itens_wikidata(nome: str, limite: int = 5) -> list[str]:
    """Q-ids candidatos para esta atração, na ordem em que o Wikidata devolve."""
    dados = consultar({"action": "wbsearchentities", "search": nome,
                       "language": "en", "type": "item", "limit": limite,
                       "format": "json"}, WIKIDATA_API)
    return [item["id"] for item in dados.get("search", [])]


def duracao_do_item(entidade: dict) -> str | None:
    """Valor CRU do P2047, com a unidade junto. Não converte: quem lê decide.

    Converter aqui repetiria o erro que o `--diagnostico` existiu para corrigir
    — decidir sozinho o que só dá para decidir olhando. E a unidade importa:
    "+3" pode ser 3 minutos ou 3 segundos, e o Q vem colado justamente por isso.
    """
    for reivindicacao in entidade.get("claims", {}).get("P2047", []):
        valor = (reivindicacao.get("mainsnak", {}).get("datavalue") or {}).get("value")
        if not isinstance(valor, dict):
            continue
        unidade = str(valor.get("unit", "")).rsplit("/", 1)[-1]
        return f"{valor.get('amount', '?')} {UNIDADES_WIKIDATA.get(unidade, '')}({unidade})"
    return None


def itens_wikidata(ids: list[str]) -> list[dict]:
    """Rótulo, descrição e duração de cada item. A descrição é o que diz o parque.

    O Wikidata descreve atração como "dark ride at Magic Kingdom" ou "roller
    coaster at Universal's Islands of Adventure" — é ela que separa a nossa
    instalação da homônima em Tóquio, sem depender de casar nome.
    """
    if not ids:
        return []
    dados = consultar({"action": "wbgetentities", "ids": "|".join(ids),
                       "props": "labels|descriptions|claims", "languages": "en",
                       "format": "json"}, WIKIDATA_API)
    saida = []
    for qid in ids:
        entidade = dados.get("entities", {}).get(qid)
        if not entidade:
            continue
        saida.append({
            "id": qid,
            "rotulo": entidade.get("labels", {}).get("en", {}).get("value", ""),
            "descricao": entidade.get("descriptions", {}).get("en", {}).get("value", ""),
            "duracao": duracao_do_item(entidade),
        })
    return saida


def relatar_wikidata(config: dict, dados: dict) -> None:
    """Sonda o Wikidata para cada atração ainda sem duração. Não grava nada."""
    achados = 0
    for parque, cfg in config["parks"].items():
        atuais = dados["rides"].get(parque, {})
        pendentes = [a for a in cfg.get("attractions", {}) if a not in atuais]
        if not pendentes:
            continue
        print(f"\n=== {parque}")
        for atracao in pendentes:
            try:
                itens = itens_wikidata(buscar_itens_wikidata(atracao))
                time.sleep(PAUSA_ENTRE_CHAMADAS_S)
            except Exception as exc:  # noqa: BLE001 — uma atração não derruba o resto
                print(f"  {atracao}\n     ! {type(exc).__name__}: {exc}")
                continue
            com_duracao = [item for item in itens if item["duracao"]]
            if not com_duracao:
                print(f"  {atracao} — nenhum dos {len(itens)} itens tem P2047")
                continue
            achados += 1
            print(f"  {atracao}")
            for item in com_duracao:
                print(f"     {item['id']}  {item['duracao']}  {item['rotulo']}"
                      f" — {item['descricao']}")
    print(f"\n{achados} atração(ões) com P2047 no Wikidata.")


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
    ap.add_argument("--diagnostico", action="store_true",
                    help="mostra o infobox cru do que ficou sem duração; não grava")
    ap.add_argument("--wikidata", action="store_true",
                    help="sonda o Wikidata (P2047) para o que ficou sem duração; não grava")
    args = ap.parse_args()

    config = monitor.load_config()
    dados = carregar_arquivo()
    dados.setdefault("rides", {})
    achadas, faltando = 0, []

    if args.wikidata:
        relatar_wikidata(config, dados)
        print("\n--wikidata: nada gravado.")
        return 0

    if args.diagnostico:
        relatar_campos_crus(config, dados)
        print("\n--diagnostico: nada gravado.")
        return 0

    for parque, cfg in config["parks"].items():
        atuais = dados["rides"].setdefault(parque, {})
        print(f"\n{parque}")
        for atracao in cfg.get("attractions", {}):
            if atracao in atuais and not args.sobrescrever:
                print(f"  ✓ {atracao} — já tem {atuais[atracao]} min")
                continue
            try:
                titulo = buscar_pagina(atracao, parque)
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
