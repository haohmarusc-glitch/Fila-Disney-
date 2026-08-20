# CLAUDE.md — contexto do projeto

## O que é

Monitor de filas dos parques de Orlando (Disney + Universal) para a viagem de 12–25/out/2026. Coleta histórico via API pública Queue-Times.com e envia alertas Telegram nos dias de parque. Roda em Docker no mesmo VPS do Premercado, mas é um projeto independente.

## Regras do projeto

1. **NUNCA** implementar automação de compra/reserva no My Disney Experience ou apps oficiais da Disney/Universal — viola ToS e arrisca os ingressos. Este projeto só lê dados públicos de fila.
2. Manter a atribuição "Powered by Queue-Times.com" visível (exigência da API gratuita).
3. IDs de parque nunca hardcoded — sempre resolver por nome via `parks.json`.
4. Dependências mínimas: hoje só `requests`. Antes de adicionar libs, avaliar stdlib.
5. O loop principal (`monitor.py:main`) nunca pode morrer por exceção de ciclo — sempre catch amplo com log.
6. Timestamps no banco em UTC ISO; conversão para horário do parque (`America/New_York`) só na exibição/análise.
7. Idioma de comentários, logs e mensagens Telegram: português (BR).

## Arquitetura

- `monitor.py` — loop de 5 min: fetch → grava SQLite → checa thresholds → alerta
- `notifier.py` — Telegram (env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
- `analyze.py` — CLI de análise do histórico
- `watchlist.json` — config declarativa (parques, atrações, thresholds, dias)
- `data/history.db` — SQLite, volume Docker, fora do git

Tabelas: `wait_times(ts, park, land, ride, wait_time, is_open)` e `alerts_sent(park, ride, sent_at)`.

## Comandos

```bash
docker compose up -d --build      # deploy
docker compose logs -f            # logs
docker compose exec fila-disney python analyze.py   # análise
```

## Datas críticas

- Disney: 13–17/out (Lightning Lane compra 3 dias antes, manual, ~7h da manhã)
- Universal: 19–23/out
- Antes de 12/out: modo coleta. Durante: modo alerta automático via `park_days`.
