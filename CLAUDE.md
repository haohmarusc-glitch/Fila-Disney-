# CLAUDE.md — contexto do projeto

## O que é

Monitor de filas dos parques de Orlando (Disney + Universal) para a viagem de 12–25/out/2026. Coleta histórico via API pública Queue-Times.com e envia alertas Telegram nos dias de parque. Roda em Docker no mesmo VPS do Premercado, mas é um projeto independente.

## Regras do projeto

1. **NUNCA** implementar automação de compra/reserva no My Disney Experience ou apps oficiais da Disney/Universal — viola ToS e arrisca os ingressos. Este projeto só lê dados públicos de fila.
2. Manter a atribuição "Powered by Queue-Times.com" visível (exigência da API gratuita).
3. IDs de parque nunca hardcoded — sempre resolver por nome via `parks.json`.
4. Dependências mínimas: hoje só `requests`, com versão fixada. Antes de adicionar libs, avaliar stdlib — os testes usam `unittest` por isso.
5. O loop principal (`monitor.py:main`) nunca pode morrer por exceção de ciclo — sempre catch amplo com log.
6. Timestamps no banco em UTC ISO; conversão para horário do parque (`America/New_York`) só na exibição/análise.
7. Idioma de comentários, logs e mensagens Telegram: português (BR).
8. Todo nome vindo da API (atração, parque, land) passa por `notifier.esc` antes
   de entrar numa mensagem: `parse_mode=HTML` + `&` cru = 400 do Telegram, e
   "Mickey & Minnie's Runaway Railway" está na watchlist.
9. Comando só é atendido de chat autorizado. O `TELEGRAM_CHAT_ID` do `.env` entra
   autorizado na criação do banco; os demais entram por `/entrar <senha>`
   (`FAMILY_ACCESS_PASSWORD`), com freio de 5 erros por hora e aviso único a quem
   não tem acesso. A regra antiga — só o `TELEGRAM_CHAT_ID` — valeu até o acesso
   familiar existir; quem escrever comando novo lê `authorized_chats`, não a env.
10. Fila de single rider / virtual não entra em **nada** que o usuário vê
    (`FILAS_IGNORADAS`): a API publica como atração separada e o match parcial
    casa com a atração real. **Já foi testado exibir num bloco à parte e não dá**
    — medido em 23/08/2026, as 19 filas paralelas somam ~18.000 leituras em 30
    dias e nenhuma com `wait_time` acima de 0. O campo é placeholder fixo, não
    dado que às vezes chega. O `is_open` também não serve: no Universal fica
    preso em `true` (963/963 leituras abertas, o que nenhum parque faz) e na
    Disney fica em ~50%, espelhando a atração-mãe. Os números estão em
    `tests/test_filas_paralelas_reais.py`; se a API mudar, o assunto reabre.
11. Toda chamada externa passa por `get_json` (retry, backoff, 429). Nunca chamar
    `requests.get` direto — com **uma** exceção escrita: `enviar_heartbeat`, que
    não busca JSON e não pode ter retry. Heartbeat retentado mente sobre o ciclo
    em que foi gerado, e um watchdog que recebe batida atrasada é pior que um que
    não recebe nada. Exceção nova só existe se entrar nesta regra.
12. Distância/tempo a pé só sai de coordenada real do `coords.json`, e duração de
    atração só sai do `duracoes.json`. A duração **quer ser** o tempo total —
    pré-show obrigatório, a atração e a folga para sair —, não o ciclo do
    brinquedo, porque é o total que responde "cabe antes de fechar". Onde há
    pré-show os dois números divergem muito: o Mission: SPACE tem 6 min de ciclo
    e 15 de total. Fonte é o TouringPlans, via `duracoes.py`; a Wikipédia foi
    abandonada em 24/08/2026 por medir o ciclo, e o Wikidata por ter o item mas
    com o P2047 vazio (números em `duracoes.json:_fontes_esgotadas`). **Mas a
    fonte não cumpre isso em tudo**: medido em 24/08/2026, 17 das 31 atrações
    com as duas medidas têm `rides - veiculo < 2`, e casos como o TRON (total=1)
    ou o Gringotts (total = ciclo) são claramente o ciclo. Onde isso acontece a
    tela subestima o compromisso e o "cabe antes de fechar" erra **para menos**,
    nunca para mais. Não foi corrigido porque corrigir exigiria número sem
    fonte; o conserto é cronometrar in loco e promover a `_ajustes` com
    proveniência, como se fez com o Rise of the Resistance. Detalhe e lista em
    `duracoes.json:_limite_da_fonte`. **Nunca misturar as duas medidas no mesmo
    campo** — se o coletor não fechar os sete parques, ele não grava nada, de
    propósito. Elas coexistem em seções rotuladas do mesmo arquivo: `rides` é o
    total e `veiculo` é o ciclo (Wikipédia, 31 atrações), e o pré-show/embarque
    exibido é a diferença entre os dois — só quando ambos existem e a diferença
    passa de 2 min, porque abaixo disso é ruído de medição, e negativa
    (Kilimanjaro) é divergência de fonte, não pré. Sem o dado, a atração aparece sem a estimativa — nunca com número
    inventado. Duração **não** entra em soma que
    ordena nada: fila e caminhada são custo, duração é o que se quer, e somá-la
    poria o Kilimanjaro Safaris (22 min de passeio) atrás de um brinquedo de 90
    segundos com a mesma fila. Ela serve para dizer o compromisso de tempo e
    para o "cabe antes de fechar".
13. Coordenada de parque vinda da API passa por sanidade (`coordenadas_sanas`): o `parks.json` já entregou o Epic Universe com longitude positiva. Ponto fora da curva é isolado com aviso, nunca corrigido em silêncio.
14. `last_updated` da API é levado a sério: leitura velha não alerta e não entra em ranking (`leitura_obsoleta`).
15. Ausência de dado **nunca** vira 0 min: `wait_time` None fica None, no banco e na mensagem.
16. Mudou comportamento? Teste em `tests/` junto. O CI barra o merge se quebrar.
17. `park_days` tem que refletir `docs/ROTEIRO.md`. Mudou o roteiro, muda os dois juntos — alertar o parque errado no dia é pior que não alertar.
18. Posição de familiar só circula com opt-in explícito (`group_sharing`), e ver
    exige compartilhar. O `/grupo` mostra parque, referência a até 400 m e há
    quanto tempo — nunca lat/lon, que num chat vira registro permanente da
    posição exata de alguém. Perder o acesso (`/sair`, `/revogar`) apaga junto a
    posição, o nome e o compartilhamento.
19. Horário de funcionamento não é cravado: sai do histórico
    (`horario_operacao`), porque muda por dia e por temporada — em outubro os
    parques esticam por causa das festas de Halloween. A hora de fechamento
    medida fica fora do "melhor do dia": ali a fila está drenando e a dica
    mandaria o grupo para um parque fechando.

## Arquitetura

`coords.json` é o banco local de coordenadas, versionado. A Overpass (`coords.py`) é enriquecimento de uma vez só, nunca dependência de runtime — o `monitor.py` não conhece a Overpass. `localizacao.py` isola tudo que fala de geografia.


- `monitor.py` — loop de 5 min: fetch → grava SQLite → checa thresholds → alerta.
  Entre um ciclo e outro fica em long polling do Telegram atendendo comandos
  (`/status`, `/parques`, `/help`) — mesma thread, sem concorrência com o SQLite
- `notifier.py` — transporte Telegram: `send`, `get_updates`, `esc` (env:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
- `analyze.py` — CLI de análise do histórico
- `site/` — o frontend (filadisney.premercadosc.com): três estáticos sem
  framework, servidos pelo Caddy do Premercado com `/api/*` repassado à API —
  mesmo domínio, sem CORS. O token da API **não** vai para o navegador: quem
  injeta o header é o Caddy, e quem protege a página é o `basic_auth` dele.
  Até 25/08/2026 o token era colado num `config.js` que o próprio Caddy servia
  como estático — qualquer um na internet lia `/config.js` e levava a
  credencial. Instalação e virada de DNS em `docs/SITE.md`. O `app.js` é testado de verdade:
  `tests/harness_site.js` monta um DOM mínimo e roda o arquivo no `node`, que
  o runner do CI já tem — o teste é pulado onde não houver, nunca falso-verde.
  Existe porque dois defeitos passaram por 469 testes de Python e só
  apareceram no celular: painel em branco com o parque fechado e vigia
  anunciando "0 min" em atração fechada
- `api_server.py` — API HTTP privada que serve o site (`/perto`, `/parque`,
  `/vigias`, `/comandos`, `/comando`, `/health`). O `/comando` roda os
  formatadores do **próprio** `monitor.py` e devolve o mesmo texto que o
  Telegram manda — o site não reimplementa `/menores` nem `/status`, porque
  dois critérios divergiriam justamente no dia em que fossem comparados dentro
  do parque. A whitelist `COMANDOS_SITE` é fechada e só tem leitura: o
  `/teste_alertas` ficou de fora por ser o único que **escreve** (dispara
  alertas para os chats), e `/vigiar`, `/entrar` e `/revogar` precisam de um
  chat de destino que o site não tem — o token é da família inteira, não de
  uma pessoa.
  Processo separado, container `fila-disney-api`, **somente leitura** no mesmo
  SQLite. Publicada pelo Caddy do Premercado em `api-filadisney.premercadosc.com`
  — ou seja, encara a internet: token por `hmac.compare_digest`, freio de chute
  de token, freio de ritmo autenticado e cache de 60s por parque
- `personagens.py` — pontos de encontro de personagens; alimenta o alerta por
  proximidade quando a família compartilha localização
- `localizacao.py` — tudo que fala de geografia (distância, rota, ranking)
- `healthcheck.py` / `healthcheck_api.py` — um por container; o da API bate em
  `/health` por HTTP, o do monitor mede se a coleta está viva
- `watchlist.json` — config declarativa (parques, atrações, thresholds, dias)
- `docs/ROTEIRO.md` — roteiro da viagem; é a fonte de verdade do `park_days`
- `coords.py` — script avulso (roda uma vez) que busca coordenadas no OpenStreetMap
- `duracoes.py` — script avulso (roda uma vez) que coleta duração das páginas
  públicas "Attraction Durations" do TouringPlans. A Queue-Times e a
  themeparks.wiki não publicam esse dado
- `coords.json` — coordenadas por atração; opcional, só o `/perto` depende dele
- `duracoes.json` — duração TOTAL de cada atração em minutos, com pré-show;
  opcional, porque a API não publica isso. Ausente ou incompleto = atração sem
  duração na tela, nunca com estimativa
- `data/history.db` — SQLite, volume Docker, fora do git

Tabelas: `wait_times(ts, park, land, ride, wait_time, is_open)`, `alerts_sent(park, ride, sent_at)` e `daily_summary(sent_on)` — esta última guarda a data (no fuso do parque) em que o resumo das 7h já saiu, para não repetir. `top_alert(id=1, sent_at)` guarda o último envio do alerta de menores filas.

`fila_watches(chat_id, park, ride, limite_min, limite_pct)` é a vigia de fila por pessoa (`/vigiar everest 40`, ou `50%` do típico do horário): no máximo 5 por chat, dispara **uma** vez e se apaga — e o modo percentual só dispara com perfil histórico suficiente, nunca com chute. `atracoes_conhecidas(park, ride, visto_em, avisado)` é o que permite avisar de atração nova sem repetir: a primeira leitura de um parque entra toda marcada como avisada, senão o primeiro ciclo depois do deploy anunciaria as 76 do Magic Kingdom de uma vez. Depois disso, nome que aparece pela primeira vez rende **um** aviso — o filtro da watchlist continua descartando em silêncio no `/status` e no alerta, que é o certo para a tela, mas agora ele avisa uma vez o que descartou.

`group_sharing` (opt-in do `/grupo`) e `chat_names` (rótulo vindo do Telegram)
não expiram por tempo: são preferência e etiqueta, não rastro. Saem no
`revogar_acesso`, junto com `user_locations` e `character_last_checks` daquele
chat — tirar o acesso e deixar a posição seria meia revogação.

Retenção não é uniforme de propósito (`maybe_maintain_db`): logs de operação
expiram em 90 dias, `wait_times` só 30 dias depois do fim da viagem — é o dado
que treina a previsão — e as tabelas de GPS (`user_locations`,
`character_last_checks`, `character_alerts`) em 7 dias, porque nenhum leitor olha
além de 180 min e é posição de gente real. O `VACUUM` não é rotina: só roda
quando há espaço morto de verdade e disco para a cópia que ele monta.

`run_cycle` devolve os payloads que buscou; o alerta de menores filas consome esse dicionário em vez de refazer o fetch. Se um parque falhou no ciclo, ele simplesmente não está no dicionário e o alerta pula a rodada.

O resumo diário lê o histórico agrupando por (dia, hora) em UTC e converte cada balde com `hora_no_parque`, nunca por um offset único: em novembro Orlando volta ao EST, e usar o offset de hoje para todo o histórico deslocaria em 1h o que foi coletado em outubro. A `analyze.py` chama a mesma função — a regra vive num lugar só, porque foi duplicá-la que produziu o `-4` cravado. A previsão também tem janela (`daily_summary.lookback_days`, 60 dias): sem ela, agrupava o histórico inteiro a cada `/resumo`.

## Comandos

```bash
python -m unittest discover -s tests -t .   # testes (stdlib, sem dependência extra)
docker compose up -d --build      # deploy
docker compose logs -f            # logs
docker compose exec fila-disney python analyze.py   # análise
```

## Datas críticas

- Disney: 13/out HS, 14/out AK, 15/out EPCOT, 17/out MK (16/out sem parque). Lightning Lane compra 3 dias antes, manual, ~7h da manhã
- Universal: 19/out IOA, 20/out USF, 21/out EU (Express Pass só no EU)
- 16, 18 e 22–25/out não têm parque: modo coleta apenas
- Antes de 12/out: modo coleta. Durante: modo alerta automático via `park_days`.
