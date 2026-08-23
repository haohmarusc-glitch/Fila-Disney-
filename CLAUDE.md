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
9. Comando só é atendido se vier do `TELEGRAM_CHAT_ID` configurado.
10. Fila de single rider / virtual não entra em alerta nem `/status`
    (`FILAS_IGNORADAS`): a API publica como atração separada, o match parcial
    casa com a atração real e o tempo vem 0 sem dado — alerta falso na certa.
11. Toda chamada externa passa por `get_json` (retry, backoff, 429). Nunca chamar `requests.get` direto.
12. Distância/tempo a pé só sai de coordenada real do `coords.json`. Atração sem coordenada aparece sem estimativa — nunca com número inventado.
13. Coordenada de parque vinda da API passa por sanidade (`coordenadas_sanas`): o `parks.json` já entregou o Epic Universe com longitude positiva. Ponto fora da curva é isolado com aviso, nunca corrigido em silêncio.
14. `last_updated` da API é levado a sério: leitura velha não alerta e não entra em ranking (`leitura_obsoleta`).
15. Ausência de dado **nunca** vira 0 min: `wait_time` None fica None, no banco e na mensagem.
16. Mudou comportamento? Teste em `tests/` junto. O CI barra o merge se quebrar.
17. `park_days` tem que refletir `docs/ROTEIRO.md`. Mudou o roteiro, muda os dois juntos — alertar o parque errado no dia é pior que não alertar.

## Arquitetura

`coords.json` é o banco local de coordenadas, versionado. A Overpass (`coords.py`) é enriquecimento de uma vez só, nunca dependência de runtime — o `monitor.py` não conhece a Overpass. `localizacao.py` isola tudo que fala de geografia.


- `monitor.py` — loop de 5 min: fetch → grava SQLite → checa thresholds → alerta.
  Entre um ciclo e outro fica em long polling do Telegram atendendo comandos
  (`/status`, `/parques`, `/help`) — mesma thread, sem concorrência com o SQLite
- `notifier.py` — transporte Telegram: `send`, `get_updates`, `esc` (env:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
- `analyze.py` — CLI de análise do histórico
- `watchlist.json` — config declarativa (parques, atrações, thresholds, dias)
- `docs/ROTEIRO.md` — roteiro da viagem; é a fonte de verdade do `park_days`
- `coords.py` — script avulso (roda uma vez) que busca coordenadas no OpenStreetMap
- `coords.json` — coordenadas por atração; opcional, só o `/perto` depende dele
- `data/history.db` — SQLite, volume Docker, fora do git

Tabelas: `wait_times(ts, park, land, ride, wait_time, is_open)`, `alerts_sent(park, ride, sent_at)` e `daily_summary(sent_on)` — esta última guarda a data (no fuso do parque) em que o resumo das 7h já saiu, para não repetir. `top_alert(id=1, sent_at)` guarda o último envio do alerta de menores filas.

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
