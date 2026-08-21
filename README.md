# Fila-Disney 🎢

Monitor de tempos de fila dos parques de Orlando (Disney World + Universal), com histórico em SQLite e alertas em tempo real via Telegram.

**Powered by [Queue-Times.com](https://queue-times.com/en-US)** — dados atualizados a cada 5 minutos.

## Como funciona

O monitor tem dois modos, escolhidos automaticamente pela data (fuso `America/New_York`):

| Modo | Quando | O que faz |
|---|---|---|
| **Coleta** | Todos os dias | Grava tempo de fila de todas as atrações dos 7 parques no SQLite a cada 5 min |
| **Alerta** | Dias listados em `park_days` | Além de gravar, envia alerta no Telegram quando uma atração da watchlist do parque do dia cai abaixo do threshold |

Nos dois modos o bot responde comandos no Telegram — dá para consultar a fila
sob demanda sem esperar alerta nenhum.

Regras de alerta: cooldown de 45 min por atração (não spamma), silêncio entre 22h e 7h, só alerta o parque programado para o dia.

## Deploy no VPS

```bash
git clone git@github.com:haohmarusc-glitch/Fila-Disney-.git
cd Fila-Disney-
cp .env.example .env   # preencher token e chat_id do Telegram
docker compose up -d --build
docker compose logs -f  # deve mostrar "Parques resolvidos: {...}"
```

### Atualizando um deploy existente

⚠️ **Uma vez só, na atualização que trouxe o usuário não-root:** o container
deixou de rodar como root, então o `data/` do host precisa mudar de dono, senão
o SQLite fica sem permissão de escrita e o monitor não sobe.

```bash
cd /root/Fila-Disney-
git pull
sudo chown -R 10001:10001 data     # só nesta atualização
docker compose up -d --build
docker compose logs --tail=20
```

Se esquecer, o log mostra `unable to open database file`. O `chown` resolve
sem perder o histórico.

Nas atualizações seguintes basta `git pull && docker compose up -d --build`.

### Criar o bot Telegram (2 min)

1. Fale com o `@BotFather` → `/newbot` → copie o token para `.env`
2. Mande `/start` pro seu bot
3. Acesse `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o `chat.id` para `.env`

## Comandos no bot

| Comando | O que faz |
|---|---|
| `/status` | Fila agora das atrações da watchlist do parque do dia |
| `/status <parque>` | Fila de qualquer parque monitorado (`/status Epcot`, `/status islands`) |
| `/resumo` | Previsão do dia pelo histórico — o mesmo texto que chega às 7h |
| `/resumo <parque>` | Previsão de um parque específico, em qualquer data |
| `/menores` | Ranking das menores filas do parque **inteiro** agora |
| `/menores <parque>` | Ranking de um parque específico |
| `/parques` | Lista os parques que o monitor resolveu na API |
| `/perto` (ou `/agora`) | Melhor atração agora por **fila + caminhada**, a partir da sua localização |
| `/health` | Estado do monitor: última coleta, parques resolvidos, tamanho do histórico |
| `/help` | Ajuda |

O `/status` consulta a API na hora — não devolve o último ciclo gravado. A
resposta vem ordenada da menor fila para a maior, com ✅ nas atrações que já
estão abaixo do threshold e 🔒 nas fechadas. A seta mostra a tendência dos
últimos 35 minutos: `↓12` caiu 12 min, `↑8` subiu 8, `→` estável. Dentro do
parque "31 min e subindo" e "31 min e caindo" são decisões opostas.

Filas de **single rider** e virtuais ficam fora do alerta e do `/status`: a API
publica cada uma como atração separada, o nome casa por match parcial com a
atração de verdade e o tempo vem 0 quando não há dado — o que viraria alerta
falso de "0 min, vai agora". Elas continuam sendo gravadas no histórico.

## Alerta das menores filas

Em dia de parque, a cada 10 minutos, o bot manda as 3 atrações da watchlist com
a menor fila naquele momento:

```
⚡ Menores filas agora · Disney Hollywood Studios · 14h32
1️⃣ Alien Swirling Saucers — 15 min ✅
2️⃣ Toy Story Mania! — 25 min ✅
3️⃣ Tower of Terror — 30 min
```

Configurável em `watchlist.json`:

```json
"top_alert": { "enabled": true, "every_minutes": 10, "count": 3, "list_size": 10, "only_park_days": true }
```

A cada 10 min das 7h às 22h dá cerca de 90 mensagens por dia de parque — suba
`every_minutes` se for demais. As quiet hours valem também para ele, então nada
chega entre 22h e 7h. Não gasta chamada extra na API: reaproveita o payload que
o ciclo de coleta já buscou.

Diferença para o `/status`: o alerta e o `/status` olham só a **sua watchlist**,
enquanto o `/menores` ranqueia o **parque inteiro** e marca com ⭐ o que está na
watchlist — serve para achar fila curta em atração que você não listou.

## Localização: `/perto`

Mande `/perto` e depois sua localização (o bot mostra um botão; também dá pelo
clipe 📎 → Localização). Ele detecta em qual parque você está e responde a
watchlist ordenada por **fila + caminhada**, com link de rota:

```
📍 Você está em Disney Hollywood Studios

🥇 Toy Story Mania! — 27 min no total
     fila 25 min ↓8 · 🚶 2 min (100 m)
🥈 Slinky Dog Dash — 31 min no total
     fila 20 min · 🚶 11 min (1000 m)

🗺️ Abrir rota até Toy Story Mania!
```

O critério é o tempo total, não a menor fila: 25 min de fila do lado ganha de
20 min do outro lado do parque.

O bot **não** consegue puxar sua posição sozinho — você compartilha quando quer.

### Coordenadas: rode uma vez

O Queue-Times não devolve lat/lon das atrações (só `id`, `name`, `is_open`,
`wait_time` e `last_updated`), então as coordenadas vêm do OpenStreetMap:

```bash
docker compose exec fila-disney python coords.py --revisar   # só relatório
docker compose exec fila-disney python coords.py             # grava coords.json
docker compose exec fila-disney python coords.py --forcar    # consulta até parques já completos
docker compose exec fila-disney python coords.py --listar    # nomes normalizados do OSM; nunca grava
docker compose exec fila-disney python coords.py --sobrescrever  # autoriza substituir existentes
```

### O banco de coordenadas é o `coords.json`

Ele é **versionado no repositório** e é a única fonte que o bot lê em runtime. A
Overpass serve para *preencher* esse arquivo, não para servi-lo:

```
Overpass  ──(uma vez, opcional)──>  coords.json  ──(sempre)──>  /perto
```

Consequências práticas: o `/perto` funciona sem rede externa nenhuma além do
Queue-Times; Overpass fora do ar não afeta o bot rodando; atração sem
coordenada aparece na lista sem estimativa de caminhada, em vez de sumir; e o
`coords.py` termina com código 0 mesmo se a Overpass não responder para parque
nenhum, porque isso não é falha do sistema.

Se preferir, dá para pular a Overpass inteira e preencher o `coords.json` à
mão, no formato `"Nome da Atração": [lat, lon]`.

O `coords.json` é gravado em `data/`, que é o volume — sobrevive ao
`docker compose up --build`. Se quiser versionar no git (para não depender de
rodar o script numa VPS nova), copie para a raiz do projeto e commite:

```bash
docker compose cp fila-disney:/app/data/coords.json ./coords.json
git add -f coords.json && git commit -m "Coordenadas das atrações" && git push
```

O monitor lê `data/coords.json` primeiro e cai no `coords.json` versionado
quando o volume está vazio — máquina nova já sobe com as coordenadas.

A Overpass é serviço público e gratuito, com política de uso moderado: o script
espaça as consultas em 20s e espera 45s no 429. Ainda assim ela pode recusar —
nesse caso **rode de novo mais tarde**, que ele continua de onde parou: o
progresso é gravado a cada parque e parque já completo é pulado.

Se algum parque vier com coordenada implausível, o script isola aquele parque e
diz qual é a correção provável — foi o caso do Epic Universe em 20/08/2026, que
o `parks.json` entregou com longitude `+81.44` (sem o sinal, cai no Nepal). Para
corrigir, edite `coords.json` na seção `"parks"` e rode de novo: **o script
preserva o que já está lá**, então correção manual não se perde.

Por segurança, coordenadas que já existem (de parque ou atração) são sempre
preservadas. `--forcar` força uma nova consulta à Overpass, mas não apaga esses
valores. Para aceitar conscientemente a substituição pelos resultados
automáticos, combine com `--sobrescrever`.

Quando uma atração não casa e nem candidato aparece, o problema não é o
casamento: é o OSM não ter devolvido aquela atração. Use `--listar` para ver os
nomes normalizados usados no casamento e confirmar, em vez de supor. Esse modo
consulta inclusive parques completos e não grava no `coords.json`.

O casamento de nomes entre OSM e Queue-Times é aproximado: o script marca
`[ OK ]`, `[ CONF ]` (confira) e `[ FALTA ]`. O que faltar pode ser preenchido à
mão no `coords.json`, no formato `"Nome da atração": [lat, lon]`. Atração sem
coordenada aparece no fim da lista, sem estimativa — **nunca com distância
inventada**.

Sem `coords.json` o monitor roda igual; só o `/perto` fica indisponível.

## Dado desatualizado

A API traz `last_updated` por atração. Leitura parada há mais de
`alert.max_staleness_minutes` (padrão 30) não gera alerta nem entra em ranking —
"5 min de fila" com dado de 3h atrás mandaria o grupo para uma fila que não
existe mais. No `/status` ela aparece como ⏳ *dado desatualizado*, e o histórico
continua gravando normalmente.

## Resumo diário das 7h

Em dia de parque, às 7h no horário de Orlando, o bot manda sozinho a previsão do
dia montada a partir do histórico já coletado: para cada atração da watchlist, a
média na **abertura** (as duas primeiras horas, a janela do rope drop), o
**pico** e a **melhor hora** do dia, ordenado pelo pico — as de cima são as de
atacar cedo.

Configurável em `watchlist.json`:

```json
"daily_summary": { "enabled": true, "hour": "07:00", "only_park_days": true }
```

Com `only_park_days: false` ele manda também nos dias sem parque, avisando que
está em modo coleta. O padrão é `true` para não virar spam diário até outubro.
Se o container subir depois das 7h, o resumo ainda sai — vale por 2 horas — e
nunca é enviado duas vezes no mesmo dia.

### Como escrever o nome do parque

O nome casa por pedaço, sem diferenciar maiúscula. Siglas **não** funcionam
(`IOA`, `USF`, `MK`) porque não são pedaço do nome que a API usa:

| Digite | Resolve para |
|---|---|
| `magic` | Disney Magic Kingdom |
| `epcot` | Epcot |
| `hollywood` | Disney Hollywood Studios |
| `animal` | Disney Animal Kingdom |
| `islands` ou `adventure` | Islands Of Adventure At Universal Orlando |
| `studios at` | Universal Studios At Universal Orlando |
| `epic` | Universal Epic Universe |

`universal`, `studios` e `disney` sozinhos casam com mais de um parque — nesse
caso o bot lista as opções em vez de escolher. O Universal Studios precisa do
`at` para não empatar com o Hollywood Studios.

Cada mensagem vale por um comando: se você mandar várias linhas de uma vez, só
a primeira é executada.

Só o `TELEGRAM_CHAT_ID` configurado é atendido; comando de qualquer outro chat
é ignorado com warning no log. Comandos mandados enquanto o container estava
fora do ar são descartados na subida, para o bot não despejar um monte de
resposta atrasada de uma vez.

## Configuração (`watchlist.json`)

- `trip`: período da viagem e timezone
- `park_days`: qual parque em qual dia (modo alerta). Segue `docs/ROTEIRO.md`; dias sem parque ficam de fora e caem no modo coleta
- `parks.<nome>.attractions`: atração → threshold em minutos. O nome faz match parcial case-insensitive com o nome da API, então "Frozen" casa com "Frozen Ever After"
- `alert`: cooldown e quiet hours

Os IDs dos parques **não são hardcoded**: o monitor resolve pelo nome consultando `https://queue-times.com/parks.json` na inicialização. Se um parque não resolver, aparece warning no log — ajuste o nome no JSON para bater com o da API.

## Testes

Sem dependência extra — `unittest` da stdlib:

```bash
python -m unittest discover -s tests -t .
```

Cobrem os casos que quebram calado: quiet hours na virada da meia-noite,
cooldown por atração e por parque, chat não autorizado, API fora do ar, HTTP 429
com `Retry-After`, JSON inválido, atração fechada, fila sem dado (que **não**
pode virar 0 min), nome de parque ambíguo, troca de EDT para EST, virada do dia
e reinício do container sem redisparar alerta.

O CI (`.github/workflows/ci.yml`) roda isso e o build da imagem em todo push e
pull request.

## Análise pré-viagem

Depois de algumas semanas coletando:

```bash
docker compose exec fila-disney python analyze.py                      # resumo geral
docker compose exec fila-disney python analyze.py "Animal Kingdom"     # melhor/pior hora por atração
docker compose exec fila-disney python analyze.py "Epcot" "Frozen"     # histograma por hora
```

Use isso para decidir onde vale pagar Lightning Lane e onde resolve com rope drop ou fim de tarde.

## O que este projeto NÃO faz

Não automatiza compras nem reservas no My Disney Experience — isso viola os termos da Disney e arrisca o ingresso. Ele só **lê dados públicos de fila** e te avisa a hora certa de ir.

## Estrutura

```
monitor.py        # loop principal: polling + persistência + alertas
docs/ROTEIRO.md   # roteiro da viagem (fonte de verdade do park_days)
notifier.py       # Telegram: envio de mensagens + leitura de comandos
analyze.py        # análise do histórico (CLI)
watchlist.json    # parques, atrações, thresholds, dias da viagem
data/history.db   # SQLite (criado em runtime, fora do git)
```

## Roadmap (backlog para Claude Code)

- [x] Comando `/status` no bot (fila atual da watchlist sob demanda)
- [x] Resumo diário automático às 7h com previsão do dia baseada no histórico
- [ ] Dashboard web opcional em `disney.premercadosc.com` (React + endpoint FastAPI lendo o SQLite)
- [ ] Detectar atração reaberta após "Down" (filas despencam nos primeiros minutos)
- [ ] Exportar histórico pós-viagem como dataset para portfólio
