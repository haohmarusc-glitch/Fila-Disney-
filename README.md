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

Para gravar no container o commit efetivamente implantado e exibi-lo em
`/health`, faça o build com:

```bash
APP_GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

Opcionalmente configure `GOOGLE_MAPS_API_KEY` no `.env` com uma chave restrita
à Routes API e ao IP da VPS. Sem a chave, `/perto` continua usando a estimativa
local por distância.

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
| `/ranking` | Maiores filas agora entre todos os parques |
| `/ranking <parque>` | Maiores filas agora em um parque específico |
| `/ranking hoje` | Atrações mais concorridas hoje, pela média do histórico |
| `/ranking semana` | Atrações mais concorridas nos últimos 7 dias |
| `/fechadas <parque>` | O que está fechado **agora**, duração observada e instabilidade |
| `/quebras <parque>` | Quais atrações quebram **mais**, pelo histórico dos últimos 30 dias |
| `/janela <parque>` | A hora em que a fila do parque cai, medida no histórico |
| `/vigiar <atração>` | Alerta de uso único quando a atração reabrir |
| `/confianca <atração>` | Compara a fila publicada com percentis equivalentes |
| `/lotacao <parque>` | Pressão estimada pelas filas e atrações fechadas |
| `/plano` | Próximas três atrações por fila + caminhada; requer GPS recente |
| `/chuva` | Opções internas confirmadas por fila + caminhada |
| `/parques` | Lista os parques que o monitor resolveu na API |
| `/perto` (ou `/agora`) | Melhor atração agora por **fila + caminhada**, a partir da sua localização |
| `/lembretes` | Prazos que ainda vão chegar (Lightning Lane, conferências) |
| `/health` | Estado do monitor: última coleta, parques resolvidos, tamanho do histórico |
| `/teste_alertas <parque>` | Ensaia **todas** as mensagens automáticas, marcadas como **TESTE**, sem gravar nada |
| `/entrar <senha>` | Libera este chat para uso familiar (5 tentativas por hora) |
| `/sair` | Remove este chat da lista de liberados |
| `/revogar <chat_id>` | Só no chat principal: tira o acesso de outro chat |
| `/help` | Ajuda |

O `/status` consulta a API na hora — não devolve o último ciclo gravado. A
resposta vem ordenada da menor fila para a maior, com ✅ nas atrações que já
estão abaixo do threshold e 🔒 nas fechadas. A seta mostra a tendência dos
últimos 35 minutos: `↓12` caiu 12 min, `↑8` subiu 8, `→` estável. Dentro do
parque "31 min e subindo" e "31 min e caindo" são decisões opostas.

O `/perto` identifica o parque pelo **contorno formado pelas atrações** no
`coords.json`, com margem curta para GPS e entradas. Isso evita que Yacht Club,
Pop Century e parques aquáticos sejam confundidos com um parque temático.

Filas de **single rider** e virtuais ficam fora do alerta e do `/status`: a API
publica cada uma como atração separada, o nome casa por match parcial com a
atração de verdade e o tempo vem 0 quando não há dado — o que viraria alerta
falso de "0 min, vai agora". Elas continuam sendo gravadas no histórico.

## Inteligência operacional

`/fechadas` não confunde madrugada ou pré-abertura com atrações quebradas. O
bot só analisa interrupções quando ao menos 25% das atrações estavam abertas e
omite estados majoritariamente obsoletos. `/vigiar` alerta somente depois de uma
transição observada `fechada → aberta`; reiniciar o container não dispara
reaberturas falsas. A watchlist do parque do dia também recebe reabertura
automática, com cooldown de 90 minutos.

`/confianca` usa P25, mediana, P75 e P90 do mesmo dia da semana e hora local,
com mínimo de 12 amostras. O resultado é contexto histórico, não “fila real”.
`/lotacao` compara a distribuição atual com o próprio parque e informa quando
fechamentos simultâneos tornam a operação instável; não é contagem de pessoas.

`/plano` guarda a localização compartilhada por até três horas e recalcula três
etapas com a fotografia atual de fila + caminhada. Não promete prever duas
horas: deve ser executado novamente depois de cada atração. `/chuva` usa uma
lista conservadora de experiências internas; ausência de metadado nunca vira
uma suposição de proteção.

O Park-to-Park USF ↔ IOA está ativo porque o ingresso da viagem permite a troca.
O cálculo inclui fila atual do Hogwarts Express, caminhada até a estação,
embarque, viagem, caminhada no outro parque e fila da atração. A troca só é
recomendada com economia estimada de pelo menos 15 minutos.

Cada container tem o **seu** healthcheck. O do monitor olha a última coleta no
banco; o da API bate em `GET /health` pelo `healthcheck_api.py`. Antes disso a
API herdava o teste da imagem e aparecia como "healthy" medindo a saúde do
monitor — inclusive com o servidor HTTP morto.

O `/health` do bot também mostra o **espaço livre em disco**, não só o tamanho do
banco. Em 23/08/2026 o disco da VPS estava em 83% por causa de 20 GB de cache de
build do Docker, com o banco ocupando 56 MB: quem olhasse só o banco não veria o
problema. Abaixo de 2 GB livres a linha vem com ⚠️. Para recuperar espaço de
cache de build, `docker builder prune` — ele não toca em imagem em uso, container
nem volume.

O SQLite executa `PRAGMA optimize` diariamente, limpa logs operacionais antigos
e preserva todo o histórico bruto durante a viagem. A retenção de 180 dias só
passa a valer 30 dias depois do término configurado da viagem.

## Janela noturna

Fogos, parada e o horário do jantar esvaziam as filas por uma janela curta —
relatos de visitantes falam em quedas de 30% a 50%. **A hora muda por parque e
por temporada**, então o monitor não crava horário nenhum: mede no próprio
histórico.

### Índice, não média de minutos

O que ele compara **não** é a fila média em minutos, e sim um índice: para cada
atração, `fila naquela hora ÷ fila média da própria atração`; o índice da hora é
a média dessas razões. `1,00` é "hora típica", `0,75` é "25% abaixo do normal".

A média bruta de minutos engana, e os dois motivos foram medidos no Hollywood
Studios em 23/08/2026:

- **cinco atrações reportam `is_open` com fila 0 as 24 horas do dia** — elas
  puxavam a média para baixo justamente nas horas de menos movimento;
- **quem está aberto muda ao longo do dia**, então a média troca de base.

Atração cujo histórico é sempre 0 fica de fora do índice: ela não diz nada sobre
lotação.

### Contra a hora típica, não contra o pico

A queda é medida contra a **mediana** das horas de operação. O perfil real do
Hollywood Studios é um platô das 10h às 20h — 21,5 min ao meio-dia contra 22,3
às 19h — e usar o máximo fazia o `max()` escolher ruído: meio minuto de
diferença elegia um "pico das 19h" e produzia uma falsa "queda de 26%". A
mediana descreve a hora típica e não se move por isso.

A busca é depois do pico de propósito: a manhã também tem fila baixa, mas ali a
decisão já é outra (rope drop), e avisar "vai agora" às 9h não ajuda ninguém.

### O aviso

Em dia de parque, quando essa hora chega, sai uma vez:

```text
🌙 Janela de fila curta — Disney Magic Kingdom
🕒 20h05 no horário do parque

A partir das 20h as filas deste parque ficam 22% abaixo de uma hora típica do
dia (o pico foi às 14h) — fogos, parada e jantar tiram gente das filas.

Menores filas da sua watchlist agora:
1️⃣ Space Mountain — 15 min ↓12 ✅
2️⃣ Haunted Mansion — 20 min ✅
```

Hora com menos de 12 leituras ou menos de 5 atrações é descartada, e parque sem
queda relevante **não gera aviso** — o monitor prefere calar a inventar uma
janela. `/janela <parque>` mostra o índice hora a hora com pico e janela
marcados, para conferir antes da viagem.

```json
"evening_alert": { "enabled": true, "lookback_days": 30, "min_samples": 12, "min_rides": 5, "min_drop_percent": 20, "count": 3 }
```

## Quem mais quebra: `/quebras`

`/fechadas` responde "o que está fechado agora". `/quebras` responde a outra
pergunta: **quais atrações fecham com mais frequência**, pelos últimos 30 dias
do seu próprio histórico.

```text
🔧 Quem mais quebra — Disney Hollywood Studios
Últimos 30 dias, só com o parque operando

1. Slinky Dog Dash — fechada em 8% das leituras · mais às 14h
     412 de 5.184 leituras
```

Serve para decidir a ordem do dia: atração que fecha com frequência é para
atacar cedo, não para deixar para o fim.

Só entram **ciclos com o parque operando** — pelo menos 25% das atrações
abertas. Sem isso, o feed noturno (que deixa tudo fechado) faria toda atração
aparecer como "quebra 60% do tempo". Abaixo de 50 ciclos o comando diz que não
há histórico suficiente em vez de inventar um ranking.

É **indisponibilidade observada, não causa**: reforma programada e pane de meia
hora contam igual. E, como o `/resumo`, ele responde sem gastar chamada na API —
é histórico puro.

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

### Ensaio antes da viagem

O bot envia **seis** mensagens por conta própria, e todas estreariam durante a
viagem: alerta de threshold, Top-3 menores filas, reabertura de atração, resumo
das 7h, janela noturna e lembrete de prazo. Um erro de formatação ou de lógica
em qualquer uma só apareceria no dia, com o grupo dentro do parque.

```text
/teste_alertas Hollywood
```

Manda as seis ao chat que pediu, cada uma com prefixo **TESTE**, montadas pelos
**mesmos formatadores da produção** — ensaio com texto copiado não ensaiaria
nada. O ensaio **não grava**: cooldown, resumo, janela e lembretes ficam como
estavam, então pode ser repetido à vontade.

Quando algo não pode ser mostrado, ele diz em vez de omitir: parque sem janela
detectada rende "ainda não há janela detectada neste parque", e parque com a
watchlist toda fechada avisa quais formatos ficaram de fora.

## Lembretes de prazo

O monitor sabe a fila, mas quem perde a janela das 7h para comprar o Lightning
Lane Multi-Pass paga em fila o dia inteiro. Esses prazos ficam em
`watchlist.json`, em `reminders`, e chegam sozinhos no horário marcado:

```json
"reminders": [
  {
    "id": "multipass-hollywood-2026-10-10",
    "date": "2026-10-10",
    "hour": "07:00",
    "text": "Abre AGORA a compra do Multi-Pass do Hollywood Studios (dia 13/10)."
  }
]
```

O `id` é a chave de "já enviei" — **nunca reaproveite um id em lembrete novo**, e
não conte com a posição na lista: reordenar não pode fazer o já enviado sair de
novo. Sem `id`, sem `text` ou com data fora do formato ISO, o monitor recusa
subir e diz qual entrada está errada.

Vale a mesma janela do resumo das 7h: se o container subir até 2h depois da hora
marcada, o lembrete ainda sai; depois disso, não. Nunca sai duas vezes. Não
depende de parque nem da API — funciona igual em dia de coleta.

`/lembretes` lista os que ainda vão chegar, com a contagem de dias.

Os prazos já cadastrados vieram do `docs/ROTEIRO.md`: conferência de horários e
refurbishments em 05/10, Multi-Pass do Hollywood Studios em 10/10 e do Magic
Kingdom em 14/10.

## Uptime Kuma

Crie no Uptime Kuma um monitor do tipo **Push**, com intervalo de 5 minutos e
grace period de 12 minutos. Copie a URL completa para o `.env`:

```env
UPTIME_KUMA_PUSH_URL=https://SEU_UPTIME_KUMA/api/push/SEU_TOKEN
```

Use uma URL que seja alcançável de dentro do container `fila-disney`. Estar na
mesma VPS não torna automaticamente o nome de outro container resolvível se os
dois projetos não compartilharem uma rede Docker.

O monitor faz o GET somente depois que todos os parques do ciclo foram
consultados e persistidos. Ciclo parcial, erro no SQLite ou processo travado não
renovam o heartbeat. URL ausente desativa o recurso; falha do próprio Kuma gera
warning, mas nunca interrompe a coleta.

Como o Kuma está na mesma VPS, esse monitor detecta falha do processo ou da
coleta, mas não queda completa da máquina ou do provedor.

## Localização: `/perto`

Mande `/perto` e depois sua localização (o bot mostra um botão; também dá pelo
clipe 📎 → Localização). Ele detecta em qual parque você está e responde a
watchlist ordenada por **fila + caminhada**, com link de rota:

```
📍 Você está em Disney Hollywood Studios

🥇 Toy Story Mania! — 27 min no total · qualidade da fila ⭐ 82
     fila 25 min ↓8 · 🚶 2 min (100 m, rota Google)
🥈 Slinky Dog Dash — 31 min no total · qualidade da fila ⭐ 64
     fila 20 min · 🚶 11 min (1000 m, estimativa interna)

🗺️ Abrir rota até Toy Story Mania!
```

O critério é o tempo total, não a menor fila: 25 min de fila do lado ganha de
20 min do outro lado do parque.

A medalha considera **fila + caminhada**. O ⭐ mede apenas a qualidade da fila
para a mesma hora e dia da semana, combinada com a tendência; por isso não muda
quando você muda de posição mantendo fila e tendência iguais. Cada caminhada
identifica se veio de `rota Google` ou de `estimativa interna`.

Rotas incompatíveis com a distância direta, duração ou tamanho do parque são
descartadas individualmente. O IOA usa folga curta de 250 m; os demais parques
mantêm tolerância maior por causa de lagos, entradas deslocadas e caminhos
sinuosos. Cada descarte é gravado em `route_rejections` e o total aparece em
`/health`, permitindo recalibrar os limites com dados reais.

Quando o ponto real de uma atração não é caminhável no mapa, `route_anchors`
guarda separadamente uma entrada conhecida pelo Google e o pequeno trecho final.
A coordenada real não é substituída. Nomes decorados pela API, como
`Revenge of the Mummy™`, são primeiro convertidos ao nome canônico da watchlist
para reutilizar corretamente coordenadas e âncoras.

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

### Fila pequena ou grande para este horário

Quando há pelo menos 12 observações da mesma atração, hora local e dia da
semana, o `/perto` compara a fila atual com P25, mediana, P75 e P90 desse grupo:

```text
≤ P25       🟢 pequena para este horário
P25–P50     🟡 abaixo do normal
P50–P75     🟠 acima do normal
P75–P90     🔴 grande para este horário
≥ P90       🔥 excepcionalmente grande
```

O componente histórico do Opportunity Score usa a mesma faixa, em vez da
média global da atração. Com poucas amostras ele fica neutro e não inventa uma
classificação. Os timestamps do SQLite são convertidos para o fuso de Orlando,
incluindo mudanças entre EST e EDT.

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

O `TELEGRAM_CHAT_ID` configurado continua autorizado automaticamente. Para
liberar familiares sem cadastrar IDs manualmente, defina uma senha longa e
exclusiva em `FAMILY_ACCESS_PASSWORD` no `.env`. Cada pessoa envia
`/entrar <senha>` uma vez no chat privado com o bot; o chat fica autorizado no
SQLite persistente. Localizações e vigias ficam separadas por chat, e respostas
e testes voltam para quem executou o comando. Sem essa variável, o acesso
continua restrito ao chat principal. Comandos mandados enquanto o container
estava fora do ar são descartados na subida, para o bot não despejar respostas
atrasadas de uma vez.

Qualquer pessoa que descubra o nome do bot consegue falar com ele, então o
`/entrar` tem freio: **5 erros por chat em 60 minutos** e aquele chat para de ser
testado até a janela passar — a senha certa também não passa durante o bloqueio,
que é o que impede acertar por insistência. Cada erro responde quantas
tentativas sobraram, e todas ficam registradas em `auth_attempts`. Acertar zera o
histórico, para quem errou de dedo antes não ficar penalizado.

Chat não autorizado recebe o aviso de acesso restrito **uma vez a cada 24h**, não
a cada mensagem: responder sempre confirma que o bot existe e transforma spam de
estranho em mensagem enviada.

Para tirar um acesso concedido:

| Comando | Quem pode | O que faz |
|---|---|---|
| `/sair` | qualquer chat liberado | remove o próprio chat da lista |
| `/revogar` | só o chat principal | lista os chats liberados |
| `/revogar <chat_id>` | só o chat principal | tira o acesso daquele chat |

O chat do `TELEGRAM_CHAT_ID` não pode ser revogado por comando — ele vem do
`.env` e é reinserido a cada subida.

## Configuração (`watchlist.json`)

- `trip`: período da viagem e timezone
- `park_days`: qual parque em qual dia (modo alerta). Segue `docs/ROTEIRO.md`; dias sem parque ficam de fora e caem no modo coleta
- `parks.<nome>.attractions`: atração com fila → threshold em minutos. O nome faz match parcial case-insensitive com o nome da API, então "Frozen" casa com "Frozen Ever After". Shows e atrações encerradas ficam fora dessa lista, pois não representam uma fila útil para alerta ou ranking.
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

O resumo das 7h descarta horas com poucas leituras — menos de 6, ou menos da
metade da hora mais coberta daquela atração. Sem esse corte, "melhor do dia"
apontava **22h** em atração que fecha às 21h: a fila drena no fechamento e as
poucas leituras restantes viravam o mínimo do dia, mandando o grupo para um
parque fechando.

A conversão para o horário do parque é feita balde a balde, pela data de cada
leitura, e não por um offset fixo: em 01/11/2026 Orlando volta ao EST, e um
`-4` cravado deslocaria em 1h todo o histórico de outubro assim que a análise
fosse rodada em novembro.

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
