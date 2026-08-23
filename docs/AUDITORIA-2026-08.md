# Auditoria — Fila-Disney (repo + site)

**Data:** 22/08/2026 · **Commit auditado:** `d0a3c1c` (`main`) · **Faltam 51 dias** para 12/10.
**Escopo:** repositório `haohmarusc-glitch/Fila-Disney-` e o site `https://filadisney.premercadosc.com`.
**Natureza:** somente leitura. Nenhum código foi alterado, nenhum serviço foi executado.

## Metodologia e limites

- Auditado o `main` remoto (`d0a3c1c`), não o checkout local — a cópia local estava 5 PRs
  atrás (#36 a #40 + o commit de personagens), sem `api_server.py` nem `personagens.py`.
- **O site não pôde ser carregado.** `filadisney.premercadosc.com` é recusado pela política
  de egresso desta sessão (403 no CONNECT, tanto via `curl` quanto via fetch da ferramenta).
  Além disso o front-end **não está neste repositório** — aqui existe apenas a API privada
  (`api_server.py`); o site é servido pela stack do Premercado (rede Docker externa
  `premercado_default`), cujo código não está acessível nesta sessão.
- Portanto: a parte "site" desta auditoria cobre o **contrato do backend** (o que a API
  entrega, exige e expõe) e traz um **checklist de verificação** para o que só dá para
  conferir com a página aberta. Nada abaixo afirma como o site se comporta na tela.

## Errata (23/08/2026)

**A API não é privada.** A seção "Site" abaixo afirmava que o container `fila-disney-api`
"só é alcançável pelas redes Docker" por não publicar porta no host. Isso está **errado**.
O Caddyfile do Premercado, conferido no dia 23/08 na própria VPS, tem:

```
api-filadisney.premercadosc.com {
    reverse_proxy fila-disney-api:8080
}
```

Ou seja: a API tem hostname próprio e responde à internet inteira. A conclusão da auditoria
foi tirada só do `docker-compose.yml` — ausência de `ports:` diz que o Python não abre porta
no host, **não** que ninguém publica o serviço. O Caddyfile mora na stack do Premercado e não
foi lido na auditoria original; ler o compose de um lado e concluir sobre a exposição do
outro foi o erro.

Consequência prática: o `WEB_API_TOKEN` é a única barreira, e A3 deixou de ser questão de
carga para ser também de superfície. Os freios de A1 (chute de token → 429) e o limite de
ritmo autenticado foram implantados por causa disto.

**Nota sobre o resto do documento.** O texto abaixo é o registro de 22/08 e ficou como
estava, exceto pela marcação de estado. O que mudou desde então:

| Item | Estado | Onde |
|---|---|---|
| A1 — acesso familiar sem freio | ✅ resolvido | `dd28bd4` (#43) |
| A2 — SQLite compartilhado | ✅ resolvido | `dd28bd4` (#43) — WAL, `busy_timeout`, API somente leitura |
| A3 — sem cache nem limite de taxa | ✅ resolvido | cache de 60s por parque + limite de ritmo autenticado |
| A4 — atribuição fora do payload | ✅ resolvido | `attribution` no JSON do `/perto` — só as mensagens do Telegram a traziam |
| M1 — fuso fixo na `analyze.py` | ✅ resolvido | `f40ef78` (#47) |
| M2 — varredura de histórico | ✅ resolvido | `9ae1332` (#48) — índice em `ts` e janela na previsão |
| M3 — retenção não alcança GPS | ✅ resolvido | expiração de 7 dias + `VACUUM` condicional |
| M4 — documentação desatualizada | ✅ resolvido | esta errata, `CLAUDE.md`, `README.md`, `ROTEIRO.md` |
| M5 — CI não exercita a API | ✅ resolvido | `.github/workflows/ci.yml` importa `api_server` e `healthcheck_api` |
| M6 — healthcheck herdado | ✅ resolvido | `healthcheck_api.py`, `b2626d7` (#49) |
| `HTTPServer` single-thread (parte de A3) | ⏳ mantido de propósito | uma requisição por vez guarda a conexão SQLite na thread que a criou; com o cache de 60s a fila local deixou de ser o gargalo |
| B1 — `raise_for_status()` duplicado | ✅ resolvido | quem não retenta 4xx é o `break` no `except` |
| B2 — módulo carregado duas vezes | ✅ resolvido | o `__main__` delega para `monitor.main()` |
| B3 — `enviar_heartbeat` fora do `get_json` | ✅ resolvido | exceção escrita na regra 11 do `CLAUDE.md` |
| B4 — GPS no log do Docker | ✅ resolvido | `log_message` corta a query string |
| B5 — sem limite de recursos e sem alerta de disco | ✅ resolvido | `mem_limit`/`cpus` nos dois serviços; aviso no Telegram dentro da manutenção diária |
| B6 — dependências e actions sem trava | ✅ resolvido | `requirements.txt` virou lockfile com hash (verificado para cp312/x86_64); actions fixadas por SHA; Dependabot cobre pip e actions |
| B7 — sem `LICENSE` nem `SECURITY.md` | ✅ resolvido | MIT e política de reporte privado |

**SHAs usados no B6**, conferidos na API do GitHub antes de fixar — cada um foi
resolvido a partir da tag e depois validado como commit existente no repositório
certo:

| Action | Versão | SHA |
|---|---|---|
| `actions/checkout` | v4 | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` | v5 | `a26af69be951a213d495a4c3e4e4022e16d87065` |

Tag é ponteiro móvel: quem controla o repositório da action pode movê-la para
outro commit, e o CI passaria a rodar código diferente sem nenhum diff no
`ci.yml`. O comentário ao lado de cada SHA diz qual versão ele representa, e o
Dependabot mantém os dois em dia.

Também vieram de fora da auditoria, achados verificando produção: cinco atrações da
watchlist estavam invisíveis para o bot por pontuação e símbolo de marca no nome da API
(`eeb190f`, `4f3c79b`) — uma por dia de parque da viagem.

## Placar

| Severidade | Quantidade | Tema dominante |
|---|---|---|
| Alta | 4 | acesso familiar sem freio, SQLite compartilhado, carga na API pública |
| Média | 7 | fuso fixo na análise, varreduras de histórico, retenção, docs, CI |
| Baixa | 7 | higiene de código, logs com GPS, limites de container, metadados do repo |

O projeto está **muito acima da média** em disciplina de engenharia: 186 testes de stdlib,
CI verde em 56 execuções, dependência única fixada, container não-root, healthcheck,
rotação de log, `hmac.compare_digest` na senha, ausência de dado nunca virando 0 min. Os
achados abaixo são de operação e de escala, não de qualidade de código.

---

## Severidade alta

### A1 — `/entrar` aceita tentativas ilimitadas e o bot responde a qualquer estranho

`monitor.py:1363` (`autenticar_familiar`) e `monitor.py:1846-1856` (`serve_commands`).

A comparação da senha é correta (`hmac.compare_digest`), mas **não há limite de tentativas,
nem atraso, nem registro de tentativa falha**. Qualquer pessoa que descubra o nome do bot
pode testar senhas na velocidade que o Telegram permitir; acertando, ganha `/status`,
`/perto`, `/plano`, `/health` e a localização do grupo.

Pior: o bot **responde a todo chat não autorizado** com `🔒 Acesso restrito à família. Use
/entrar SUA_SENHA.` — o que confirma a existência do bot, ensina o formato do comando e
transforma cada mensagem de estranho numa resposta enviada (custo e ruído).

Como o repositório é público, o desenho do `/entrar` também é público. Isso não é um
problema por si só, mas remove qualquer valor de obscuridade.

**O que fazer:** contar falhas por `chat_id` numa tabela, bloquear após ~5 tentativas por
hora, deixar de responder ao chat não autorizado depois da primeira negativa, logar as
tentativas com o `chat_id`, e criar `/sair` (ou `/revogar <chat_id>`) — hoje um acesso
concedido não tem como ser retirado sem mexer no banco à mão.

### A2 — Dois processos gravam o mesmo SQLite, sem WAL e sem `busy_timeout`

`monitor.py:109` e `api_server.py:91` — os dois chamam `monitor.init_db()`, que abre a
conexão com `sqlite3.connect(DB_PATH)` (journal padrão, timeout padrão de 5s) e **escreve**
(cria tabelas, insere o chat principal, migra `user_location` → `user_locations`).

Desde o `docker-compose.yml` atual são dois containers sobre o mesmo `./data`. Consequências:

- O container da API grava no banco só para subir — não precisa disso, ele apenas lê.
- Sem WAL, leitor e escritor se bloqueiam. O ciclo de coleta grava ~350-500 linhas a cada
  5 min (rápido), mas `maybe_maintain_db` (`monitor.py:1993`) faz `DELETE` em lote e, depois
  da viagem, `DELETE FROM wait_times` sobre milhões de linhas — durante esse tempo as
  requisições do site batem em `database is locked` e viram HTTP 503.

**O que fazer:** `PRAGMA journal_mode=WAL` e `PRAGMA busy_timeout=5000` na criação da
conexão; abrir a conexão da API em modo somente-leitura
(`sqlite3.connect("file:...?mode=ro", uri=True)`), com um caminho de inicialização que não
tente criar tabela nenhuma.

### A3 — A API do site não tem cache, nem limite de taxa, e atende uma requisição por vez

`api_server.py:95-99`. `HTTPServer` é single-thread por natureza, e cada `GET /perto`
dispara `monitor.fetch_queue_times` ao vivo (`api_server.py:41`) — que, no pior caso, são
3 tentativas de 15s com backoff. Uma requisição lenta segura todas as outras.

Com 8 pessoas dentro do parque atualizando a tela, isso é ao mesmo tempo uma fila local e
carga extra sobre uma API pública gratuita. O monitor já faz 7 chamadas a cada 5 min
(~2.016/dia); cada `/ranking` sem argumento soma outras 7; o site soma uma por
carregamento, sem reaproveitar nada.

**O que fazer:** cache de payload por parque com TTL de 30-60s, compartilhado entre
`/perto` do site e os comandos; `ThreadingHTTPServer` (ou um limite explícito de requisições
por token/minuto); e um `HTTP_TIMEOUT` menor no caminho web, onde esperar 45s não faz
sentido.

### A4 — A atribuição "Powered by Queue-Times.com" não sai no payload da API

`api_server.py:52` devolve `{"park", "items", "source": "fila-disney-vps"}`. Todas as
mensagens do Telegram carregam a atribuição; **o JSON do site não carrega nenhuma**.

A regra 2 do `CLAUDE.md` e os termos da API gratuita exigem a atribuição visível. Se a
página não a exibe por conta própria, o projeto está fora de conformidade justamente na
superfície mais visível.

**O que fazer:** incluir `"attribution": "Powered by Queue-Times.com"` (e o link) no
payload, e confirmar que o site renderiza isso. Ver também o item S2 no checklist do site.

---

## Severidade média

### M1 — `analyze.py` usa offset de fuso fixo, contra a regra 6 do projeto

`analyze.py:20`: `TZ_OFFSET = -4`. O `monitor.py` faz a coisa certa
(`park_utc_offset_horas`, `monitor.py:1173`), e o próprio `CLAUDE.md` explica por que offset
cravado é armadilha. A partir de 01/11/2026 Orlando volta ao EST e **toda análise
pré-viagem sai 1h deslocada** — inclusive a que decide rope drop e Lightning Lane.

**O que fazer:** trocar por `ZoneInfo("America/New_York")` ou reaproveitar
`monitor.park_utc_offset_horas`.

### M2 — Consultas de histórico varrem a tabela inteira e vão piorando

- `ranking_historico` (`monitor.py:684`) filtra por `ts` sem parque; os índices existentes
  são `(park, ride, ts)` e `(park, ts)` — **não há índice por `ts` sozinho**, então
  `/ranking hoje` e `/ranking semana` fazem varredura completa.
- `previsao_por_atracao` (`monitor.py:1202`) agrupa **todo o histórico do parque**, sem
  recorte de período. É o que roda no resumo automático das 7h e em todo `/resumo`.

Volume esperado: ~350-500 linhas por ciclo × 288 ciclos = **100-140 mil linhas por dia**.
Da data de hoje até o fim da viagem, algo entre 6 e 9 milhões de linhas. Tudo isso roda na
mesma thread do ciclo de coleta, então uma consulta lenta atrasa a coleta e o atendimento
de comandos.

**O que fazer:** índice em `ts` (ou `(ts, park)`); recortar a previsão aos últimos ~60 dias;
e, se ficar apertado, materializar uma tabela agregada por `(park, ride, hora)`.

### M3 — Retenção não alcança os dados de localização, e o banco nunca encolhe

`maybe_maintain_db` (`monitor.py:1993`) limpa `reopen_alerts`, `route_rejections` e
`alerts_sent` com 90 dias, e só mexe em `wait_times` **30 dias depois do fim da viagem**.
Ficam de fora, para sempre:

- `user_locations` — última posição GPS de cada familiar;
- `character_last_checks` — posição de cada verificação de personagens;
- `character_alerts` — histórico de quem foi alertado, onde e quando.

Nenhum `VACUUM` é executado, então mesmo o que é apagado não devolve espaço em disco.

**O que fazer:** expirar as tabelas de localização (24-72h já bastam para o `/plano`, que
só usa 3h), e rodar `VACUUM` uma vez, depois da limpeza pós-viagem.

### M4 — Documentação desatualizada em três frentes ao mesmo tempo

- `README.md:443` ainda lista **"Dashboard web opcional"** como item de backlog não feito —
  enquanto a API está no `main` e o site está no ar. O README não documenta `api_server.py`,
  `WEB_API_TOKEN`, `FAMILY_ACCESS_PASSWORD`, o serviço `fila-disney-api`, nem a dependência
  da rede externa `premercado_default`. Quem for reimplantar numa VPS nova não sobe o site.
- `CLAUDE.md` não conhece `api_server.py` nem `personagens.py`, e a **regra 9** ("comando só
  é atendido se vier do `TELEGRAM_CHAT_ID` configurado") foi deliberadamente superada pelo
  acesso familiar. Regra desatualizada em arquivo de regras é pior que regra ausente.
- `docs/ROTEIRO.md` ainda cita "Le Cirque Arcanus" e implicitamente o "Hollywood Rip Ride
  Rockit" como entradas da watchlist; ambos foram removidos em `4452122` ("Remove show e
  atração encerrada"). A remoção parece certa — o texto é que ficou para trás.

### M5 — O CI não exercita o segundo container

`.github/workflows/ci.yml`: o smoke test dentro da imagem importa
`monitor, notifier, localizacao, coords, healthcheck, analyze` — **não importa `api_server`
nem `personagens`**. O comando real do container da API (`python -u api_server.py`) não é
executado em lugar nenhum do CI.

O `tests/test_empacotamento.py` garante que todo `.py` da raiz entra no `COPY`, o que cobre
o acidente original. Mas o smoke test existe justamente para pegar o que o `docker build`
não pega — um erro de import em `api_server.py` passa verde e só aparece com o site fora do ar.

Também: `tests/test_web_api.py` cobre `_number` e `build_perto_payload`, mas **não cobre a
autenticação** — não há teste para 401 sem token, token errado, ou rota inexistente.

### M6 — O container da API herda o healthcheck errado

O `HEALTHCHECK` do `Dockerfile` roda `healthcheck.py`, que verifica se **a coleta** está
recente no banco. O serviço `fila-disney-api` herda esse healthcheck: ele fica "healthy"
enquanto o monitor coleta, mesmo que o servidor HTTP esteja morto — e ficaria "unhealthy"
por culpa do monitor mesmo com a API perfeita.

Nos dois casos, o Docker não reinicia container "unhealthy" por conta própria: com
`restart: unless-stopped`, o healthcheck é informativo, não corretivo.

**O que fazer:** `healthcheck` próprio no serviço da API, batendo em `GET /health`; e, se
quiser reinício automático, um autoheal ou um monitor Push do Kuma apontando para a API.

### M7 — Token da API comparado com `!=`

`api_server.py:68`: `supplied != f"Bearer {TOKEN}"`. A senha familiar já usa
`hmac.compare_digest`; a API deveria usar o mesmo. O vazamento por tempo é pequeno numa
rede local, mas a correção é de uma linha e elimina a inconsistência.

---

## Severidade baixa

| # | Onde | O quê |
|---|---|---|
| B1 | `monitor.py:386-387` | `raise_for_status()` duplicado — a primeira chamada, dentro do `if 400 <= status < 500`, é redundante. |
| B2 | `localizacao.py:22` ↔ `monitor.py:27` | Rodando `python monitor.py`, o monitor é carregado **duas vezes** (como `__main__` e como `monitor`). Hoje é inofensivo porque só há constantes; qualquer estado de módulo futuro (cache, conexão) passaria a existir em duas cópias divergentes. |
| B3 | `monitor.py:1982` | `enviar_heartbeat` chama `requests.get` direto, contornando `get_json` — contraria a regra 11 do `CLAUDE.md`. É defensável (não é JSON e não deve ter retry), mas a exceção merece estar escrita na regra. |
| B4 | `api_server.py:85` | `log_message` registra o path completo, que contém `lat`/`lon` — GPS da família nos logs do Docker. Logar só o path sem query. |
| B5 | `docker-compose.yml` | Sem limites de memória/CPU nos dois serviços; sem alerta de disco. O `/health` mostra o tamanho do banco, mas ninguém é avisado quando ele cresce. |
| B6 | `.github/workflows/ci.yml` | Actions não fixadas por SHA, sem Dependabot, `requirements.txt` sem hashes. Risco baixo num repo pessoal, custo de correção também baixo. |
| B7 | raiz | Repositório **público**, sem `LICENSE` e sem `SECURITY.md`. O `.env` está corretamente fora do git e nenhum segredo vazou; o que é público é o desenho do acesso familiar — o que reforça A1. |

---

## Site `filadisney.premercadosc.com`

### O que dá para afirmar sem abrir a página

> ⚠️ **Corrigido em 23/08** — ver Errata. A frase a seguir sobre alcance está errada: a API
> tem hostname próprio (`api-filadisney.premercadosc.com`) e encara a internet.

**A favor:** o serviço da API **não publica porta no host** (`docker-compose.yml` não tem
`ports:`) — ~~só é alcançável pelas redes Docker `default` e `premercado_default`~~. Isso é o
desenho certo: quem fala com a internet é o Caddy do Premercado, não o Python. `/perto` exige
`Authorization: Bearer`, valida `lat`/`lon` por faixa e finitude (`api_server.py:20`), recusa
posição fora dos parques, e responde `no-store` + `nosniff`. Erro interno vira 503 genérico,
sem stack trace. Para uma ferramenta familiar, o contrato do backend é sóbrio.

**Contra:** não há CORS, nem rate limit, nem cache (A3), nem atribuição no payload (A4), e o
`/health` é público sem token — devolve apenas `{"ok": true, "service": ...}`, o que é
aceitável, mas confirma a existência do serviço a quem tiver o caminho.

### Checklist do que precisa ser verificado na página

| # | Verificar | Por que importa |
|---|---|---|
| S1 | **Onde mora o `WEB_API_TOKEN`.** Se o JavaScript do navegador envia o `Bearer`, o token é público — qualquer visitante lê no devtools. O certo é o Caddy injetar o header no `reverse_proxy` e o front nunca conhecer o token. | É o item de segurança número 1 do site. |
| S2 | **Atribuição "Powered by Queue-Times.com" visível** na página. | Exigência da API gratuita e regra 2 do projeto (ver A4). |
| S3 | **A página é indexável?** Ferramenta familiar com dados de fila não deveria estar no Google. Conferir `robots.txt` / `noindex` e se há alguma autenticação além do token de servidor. | Hoje nada no repositório restringe quem acessa a página. |
| S4 | **Headers do Caddy:** HTTPS obrigatório, HSTS, CSP, `X-Frame-Options`. | O Caddyfile do Premercado não está neste repo; a API não emite nada disso além de `nosniff`. |
| S5 | **Geolocalização no navegador:** exige HTTPS e permissão explícita. O que a tela mostra quando o usuário nega, quando o GPS é impreciso, ou quando está fora dos parques (a API responde **400** com "localização fora dos parques monitorados")? | Uma tela de "erro 400" dentro do parque é pior que não ter o recurso. |
| S6 | **Fila sem dado ≠ 0 min.** O payload devolve `wait: null`; a regra 15 do projeto vale igualmente na tela. | É a regra mais importante do projeto, e ela agora depende de código que não está aqui. |
| S7 | **Comportamento com 503** (API fora, Queue-Times fora) e com resposta lenta — ver A3. | Dentro do parque, no 4G, uma tela travando 45s é o cenário real. |
| S8 | **Conferir o que mais o Caddy expõe** do container `fila-disney-api` além de `/perto` e `/health`. | Superfície mínima. |

Assim que o site puder ser carregado (fora desta sessão, ou com o domínio liberado no
proxy), esses oito itens são o roteiro.

---

## O que está bem feito — e não deve ser mexido

- **186 testes** em `unittest` puro, cobrindo exatamente os casos que quebram calado:
  quiet hours na virada da meia-noite, EDT→EST, 429 com `Retry-After`, JSON inválido,
  fila sem dado, chat não autorizado, reinício sem redisparar alerta.
- `tests/test_empacotamento.py` — teste nascido de um incidente real (`localizacao.py` fora
  do `COPY`), que hoje garante que todo módulo da raiz entra na imagem.
- Loop principal blindado: `run_cycle`, top alert, resumo e manutenção cada um no seu
  `try/except`, exatamente como manda a regra 5.
- Dado obsoleto (`leitura_obsoleta`), fila paralela (`FILAS_IGNORADAS`) e ausência de dado
  tratados com rigor — as três armadilhas que gerariam alerta falso.
- Sanidade de coordenada (`coordenadas_sanas`) com o caso real do Epic Universe documentado
  no código, corrigido explicitamente e nunca em silêncio.
- Segredos fora do git, container não-root, log rotacionado, dependência única fixada,
  APP_GIT_SHA gravado na imagem e exibido em `/health`.

---

## Ordem sugerida, pensando na viagem

**Antes de 12/10 (obrigatório):**

1. A1 — freio no `/entrar` e silêncio para chat não autorizado.
2. A2 — WAL + `busy_timeout`; API em modo somente-leitura.
3. A3 — cache de 30-60s por parque e limite de requisições no site.
4. A4/S1/S2 — atribuição no payload e confirmação de onde mora o token do site.
5. M2 — índice em `ts` e recorte de janela na previsão (o resumo das 7h dos dias de parque
   depende disso não travar).

**Antes do fim de setembro (importante):**

6. M1 — fuso da `analyze.py` (a análise pré-viagem é feita agora).
7. M5/M6 — smoke test do `api_server` no CI e healthcheck HTTP no container da API.
8. M4 — README, CLAUDE.md e ROTEIRO.md alinhados com o que já está em produção.

**Depois da viagem:**

9. M3 — expiração das tabelas de GPS e `VACUUM`.
10. Itens B1-B7.
