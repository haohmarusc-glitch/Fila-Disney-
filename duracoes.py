"""Enriquecimento de durações — NÃO é etapa obrigatória do sistema.

O banco de durações é o `duracoes.json`, versionado no repositório. O bot lê só
ele; o TouringPlans existe para PREENCHER esse arquivo, não para servi-lo. Se o
site estiver fora, o que já está gravado continua valendo — só não ganha
duração nova. Atração sem entrada aparece sem duração, nunca com estimativa
(regra 12).

## Por que TouringPlans, e não a Wikipédia

A primeira versão deste script lia o campo `duration` do infobox da Wikipédia.
Funcionava, chegou a 31 de 54, e media a coisa ERRADA: o infobox traz o ciclo
da atração, e o que a regra 12 pede é o compromisso de tempo. As duas coisas só
coincidem onde não há pré-show.

    Seven Dwarfs Mine Train     ciclo  3 min     total  3 min
    TRON Lightcycle / Run       ciclo  1 min     total  1 min
    Big Thunder Mountain        ciclo  3 min     total  7 min
    Space Mountain              ciclo  3 min     total 10 min
    Mario Kart                  ciclo  5 min     total 12 min
    Mission: SPACE              ciclo  6 min     total 15 min

O TouringPlans publica o total — pré-show obrigatório, a atração e a folga para
sair — que é exatamente o número que decide "cabe antes de fechar" e o que
aparece na tela. Onde há briefing longo, como o Mission: SPACE, a diferença é
de 2,5x: dizer 6 min ali era subestimar em nove minutos.

A troca foi em bloco, não parcial, e de propósito: misturar as duas medidas
daria um arquivo em que `3` e `18` significam coisas diferentes e ninguém sabe
qual é qual olhando. Pela mesma razão o coletor da Wikipédia foi REMOVIDO em
vez de virar alternativa — ferramenta que produz dado incompatível com o
arquivo é armadilha, não plano B. O histórico do git guarda o código, e o
`duracoes.json:_fontes_esgotadas` guarda o que aquela investigação apurou.

## Uso

    docker compose exec fila-disney python duracoes.py --revisar   # só relatório
    docker compose exec fila-disney python duracoes.py             # grava
    docker compose exec fila-disney python duracoes.py --cru       # sem mapear

O relatório mostra cada duração com o nome que veio do site ao lado do nome
canônico, para dar para conferir o casamento antes de aceitar. Uma página que
volte com zero atrações é ERRO em voz alta, não silêncio: é assim que mudança
de layout aparece, em vez de virar arquivo vazio.
"""
import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import monitor

BASE = "https://touringplans.com"
# Uma vez só, sete páginas, com pausa: mesma postura do `coords.py` com a
# Overpass. Isto é enriquecimento avulso, nunca dependência de runtime — o
# `monitor.py` não conhece o TouringPlans.
PAUSA_ENTRE_PAGINAS_S = 2
DURACOES_PATH = monitor.DURACOES_PATH

# Quantas atrações cada página listava em 24/08/2026. Não é meta a bater — é
# alarme de regressão do parser: menos que isso significa regex comendo
# entrada, não parque encolhendo. Margem de 20% para flutuação sazonal.
CONTAGENS_REFERENCIA = {
    "Disney Magic Kingdom": 76,
    "Epcot": 94,
    "Disney Hollywood Studios": 48,
    "Disney Animal Kingdom": 34,
    "Universal Studios At Universal Orlando": 46,
    "Islands Of Adventure At Universal Orlando": 34,
    "Universal Epic Universe": 19,
}

# A chave é o nome do parque na watchlist; o valor, o caminho no site.
PAGINAS = {
    "Disney Magic Kingdom": "/magic-kingdom/attractions/duration",
    "Epcot": "/epcot/attractions/duration",
    "Disney Hollywood Studios": "/hollywood-studios/attractions/duration",
    "Disney Animal Kingdom": "/animal-kingdom/attractions/duration",
    "Universal Studios At Universal Orlando":
        "/universal-studios-florida/attractions/duration",
    "Islands Of Adventure At Universal Orlando":
        "/islands-of-adventure/attractions/duration",
    "Universal Epic Universe": "/epic-universe/attractions/duration",
}

# A página numera as atrações e põe rótulo e valor em LINHAS SEPARADAS. Medido
# em 24/08/2026, no Magic Kingdom:
#
#     '4. Big Thunder Mountain Railroad'
#     'in Frontierland'
#     '(4.5/5 · 6,633 reviews)'
#     'Tame, western-mining-themed roller coaster'
#     'Duration:'
#     '7 min'
#
# A primeira versão deste parser esperava "Duration: 7 min" grudado e voltou
# ZERO nos sete parques. O rótulo solto não tem número e o valor solto não tem
# rótulo — nenhum dos dois casava sozinho.
_ITEM = re.compile(r"^\d+\.\s+(.+?)(?:\s+in\s+([A-Z][^a-z]*))?$")
_ROTULO = re.compile(r"^duration:?$", re.I)
_VALOR = re.compile(r"^(\d+)\s*(min|minutes?|hr|hours?)\b", re.I)
# Forma grudada: não é a que o site usa hoje, mas custa uma linha aceitar as
# duas e evita voltar aqui se alguma das sete páginas divergir.
_DURACAO_JUNTA = re.compile(r"Duration:\s*(\d+)\s*(min|minutes?|hr|hours?)\b", re.I)


class _SoTexto(HTMLParser):
    """Extrai o texto visível. Substitui o BeautifulSoup, que seria dependência
    nova para uma tarefa que a stdlib faz (regra 4)."""

    def __init__(self):
        super().__init__()
        self.pedacos = []
        self._ignorar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._ignorar += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._ignorar:
            self._ignorar -= 1

    def handle_data(self, data):
        if not self._ignorar and data.strip():
            self.pedacos.append(data.strip())


def texto_visivel(html: str) -> list[str]:
    """Linhas de texto da página, sem marcação."""
    parser = _SoTexto()
    parser.feed(html)
    return parser.pedacos


def minutos(quantidade: str, unidade: str) -> int:
    """Minutos. Hora vira 60 — o Bibbidi Bobbidi Boutique aparece como 45 min,
    mas nada garante que nenhuma página use `1 hr`."""
    return int(quantidade) * (60 if unidade.lower().startswith("h") else 1)


def duracoes_da_pagina(html: str) -> dict[str, int]:
    """{nome no site: minutos}. Nome sem duração até a próxima atração some."""
    encontradas = {}
    atual = None
    esperando_valor = False
    for linha in texto_visivel(html):
        if esperando_valor:
            esperando_valor = False
            casado = _VALOR.match(linha)
            if casado and atual:
                encontradas[atual] = minutos(*casado.groups())
                atual = None
            continue
        if _ROTULO.match(linha):
            esperando_valor = True
            continue
        casado = _DURACAO_JUNTA.search(linha)
        if casado and atual:
            encontradas[atual] = minutos(*casado.groups())
            atual = None
            continue
        item = _ITEM.match(linha)
        if item:
            atual = item.group(1).strip()
    return encontradas


def e_pavilhao(nome: str) -> bool:
    """Pavilhão é lugar, não atração — e entra em conflito com a atração dele.

    O TouringPlans lista os pavilhões como itens próprios, com o tempo de
    ATRAVESSAR: os onze do World Showcase e também o "Test Track Pavilion", que
    casava com "Test Track" e disputava com a atração de verdade ("Test Track
    presented by General Motors"). O resultado era o Test Track ficar sem
    duração por um empate que nunca devia ter existido.

    Perde-se uma atração da watchlist que se chame "... Pavilion" — nenhuma se
    chama, e se um dia se chamar ela aparece como ausente no relatório, não
    como número errado.
    """
    return nome.strip().lower().endswith("pavilion")


def mapear_para_watchlist(cruas: dict[str, int], park_cfg: dict) -> tuple[dict, list]:
    """Casa os nomes do site com os canônicos da watchlist.

    Devolve (durações, conflitos). Duas entradas do site podem cair na mesma
    atração — "Mission: SPACE Green" e "Mission: SPACE Orange" são uma linha só
    na watchlist. Concordando, entra; divergindo, fica de fora e é reportado,
    porque não dá para saber qual das duas o visitante vai pegar. É a mesma
    regra que valia para as pistas Alpha e Omega do Space Mountain.
    """
    candidatos: dict[str, dict[str, int]] = {}
    for nome_site, minutos_ in cruas.items():
        if e_pavilhao(nome_site):
            continue
        canonico = monitor.nome_watchlist(park_cfg, nome_site)
        if canonico:
            candidatos.setdefault(canonico, {})[nome_site] = minutos_

    duracoes, conflitos = {}, []
    for canonico, achados in candidatos.items():
        valores = set(achados.values())
        if len(valores) == 1:
            duracoes[canonico] = (valores.pop(), sorted(achados)[0])
        else:
            conflitos.append((canonico, achados))
    return duracoes, conflitos


def coletar(parque: str, caminho: str) -> dict[str, int]:
    """Durações cruas de uma página, pelo `get_texto` do monitor (regra 11)."""
    return duracoes_da_pagina(monitor.get_texto(BASE + caminho))


def carregar_arquivo() -> dict:
    try:
        with open(DURACOES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"rides": {}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revisar", action="store_true", help="só relatório, não grava")
    ap.add_argument("--cru", action="store_true",
                    help="mostra o que veio do site sem mapear para a watchlist")
    args = ap.parse_args()

    config = monitor.load_config()
    dados = carregar_arquivo()
    dados.setdefault("rides", {})
    total, vazias = 0, []

    for parque, caminho in PAGINAS.items():
        print(f"\n{parque}")
        try:
            cruas = coletar(parque, caminho)
            time.sleep(PAUSA_ENTRE_PAGINAS_S)
        except Exception as exc:  # noqa: BLE001 — uma página não derruba o resto
            print(f"  ! falha: {type(exc).__name__}: {exc}")
            vazias.append(parque)
            continue

        referencia = CONTAGENS_REFERENCIA.get(parque, 0)
        if len(cruas) < referencia * 0.8:
            # Página que volta vazia OU muito abaixo do medido é mudança de
            # layout, não parque encolhendo. Passar batido geraria um arquivo
            # menor sem ninguém notar — o parser da v1 teria sido pego por isto.
            print(f"  ! {len(cruas)} atrações lidas, referência era {referencia} — "
                  "o layout da página provavelmente mudou")
            vazias.append(parque)
            continue

        if args.cru:
            for nome, m in sorted(cruas.items(), key=lambda i: -i[1]):
                print(f"  {m:>3} min  {nome}")
            continue

        achadas, conflitos = mapear_para_watchlist(cruas, config["parks"][parque])
        for canonico, (m, nome_site) in sorted(achadas.items()):
            # O nome do site vai junto quando difere: é a unica forma de conferir
            # que "Tower of Terror" casou com a torre e nao com outra coisa.
            origem = "" if nome_site == canonico else f"   [{nome_site}]"
            print(f"  + {canonico} — {m} min{origem}")
        for canonico, opcoes in conflitos:
            detalhe = ", ".join(f"{n} = {v} min" for n, v in sorted(opcoes.items()))
            print(f"  ? {canonico} — versões divergem, fica sem duração ({detalhe})")

        em_conflito = {c for c, _ in conflitos}
        faltando = [a for a in config["parks"][parque].get("attractions", {})
                    if a not in achadas and a not in em_conflito]
        for atracao in sorted(faltando):
            print(f"  – {atracao} — não veio na página")

        colhidas = {c: m for c, (m, _) in achadas.items()}
        # Ajuste manual vence o site, sempre. O TouringPlans erra às vezes —
        # o Rise of the Resistance saiu com 7 min quando o número divulgado na
        # abertura é 18 — e a correção verificada não pode evaporar na próxima
        # coleta. Cada ajuste declara a proveniência no próprio arquivo.
        for atracao, ajuste in dados.get("_ajustes", {}).get(parque, {}).items():
            if atracao in colhidas and colhidas[atracao] != ajuste["minutos"]:
                print(f"  ✎ {atracao} — site diz {colhidas[atracao]} min, "
                      f"mantido ajuste de {ajuste['minutos']} min ({ajuste['fonte']})")
                colhidas[atracao] = ajuste["minutos"]
        dados["rides"][parque] = colhidas
        total += len(achadas)
        print(f"  {len(achadas)} de {len(config['parks'][parque]['attractions'])} "
              f"da watchlist ({len(cruas)} atrações na página)")

    if args.cru:
        return 0

    print(f"\n{total} de 54 durações.")
    if vazias:
        # Gravar com página faltando deixaria metade do arquivo em TouringPlans
        # (total, com pré-show) e metade no que estava antes. Arquivo em que o
        # mesmo campo significa duas coisas é pior que arquivo desatualizado:
        # o desatualizado a gente sabe que está velho.
        print("\nNADA GRAVADO — estas páginas não renderam:")
        for parque in vazias:
            print(f"  {parque}")
        print("Gravar sem elas misturaria duas medidas no mesmo arquivo. "
              "Resolva e rode de novo.")
        return 1

    if args.revisar:
        print("\n--revisar: nada gravado.")
        return 0
    Path(DURACOES_PATH).write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{DURACOES_PATH} gravado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
