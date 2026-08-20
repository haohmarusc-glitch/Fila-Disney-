# Fila-Disney 🎢

Monitor de tempos de fila dos parques de Orlando (Disney World + Universal), com histórico em SQLite e alertas em tempo real via Telegram.

**Powered by [Queue-Times.com](https://queue-times.com/en-US)** — dados atualizados a cada 5 minutos.

## Como funciona

O monitor tem dois modos, escolhidos automaticamente pela data (fuso `America/New_York`):

| Modo | Quando | O que faz |
|---|---|---|
| **Coleta** | Todos os dias | Grava tempo de fila de todas as atrações dos 7 parques no SQLite a cada 5 min |
| **Alerta** | Dias listados em `park_days` | Além de gravar, envia alerta no Telegram quando uma atração da watchlist do parque do dia cai abaixo do threshold |

Regras de alerta: cooldown de 45 min por atração (não spamma), silêncio entre 22h e 7h, só alerta o parque programado para o dia.

## Deploy no VPS

```bash
git clone git@github.com:haohmarusc-glitch/Fila-Disney-.git
cd Fila-Disney-
cp .env.example .env   # preencher token e chat_id do Telegram
docker compose up -d --build
docker compose logs -f  # deve mostrar "Parques resolvidos: {...}"
```

### Criar o bot Telegram (2 min)

1. Fale com o `@BotFather` → `/newbot` → copie o token para `.env`
2. Mande `/start` pro seu bot
3. Acesse `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o `chat.id` para `.env`

## Configuração (`watchlist.json`)

- `trip`: período da viagem e timezone
- `park_days`: qual parque em qual dia (modo alerta)
- `parks.<nome>.attractions`: atração → threshold em minutos. O nome faz match parcial case-insensitive com o nome da API, então "Frozen" casa com "Frozen Ever After"
- `alert`: cooldown e quiet hours

Os IDs dos parques **não são hardcoded**: o monitor resolve pelo nome consultando `https://queue-times.com/parks.json` na inicialização. Se um parque não resolver, aparece warning no log — ajuste o nome no JSON para bater com o da API.

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
notifier.py       # envio Telegram
analyze.py        # análise do histórico (CLI)
watchlist.json    # parques, atrações, thresholds, dias da viagem
data/history.db   # SQLite (criado em runtime, fora do git)
```

## Roadmap (backlog para Claude Code)

- [ ] Comando `/status` no bot (fila atual da watchlist sob demanda)
- [ ] Resumo diário automático às 7h com previsão do dia baseada no histórico
- [ ] Dashboard web opcional em `disney.premercadosc.com` (React + endpoint FastAPI lendo o SQLite)
- [ ] Detectar atração reaberta após "Down" (filas despencam nos primeiros minutos)
- [ ] Exportar histórico pós-viagem como dataset para portfólio
