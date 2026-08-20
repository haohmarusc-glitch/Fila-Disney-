# Roteiro dos Parques — Flórida 2026

Fonte de verdade do `park_days` em `watchlist.json`. Extraído dos documentos de
planejamento *Roteiros dos Parques* e *Roteiro Guia dos Parques* (Opção B,
elaborados em 17/08/2026). Grupo de 8 pessoas, 12 a 25 de outubro, casa em
Kissimmee + 2 noites em Miami.

## Agenda

| Data | Parque / atividade | Lotação prevista | Furas-fila | Alerta ativo? |
|---|---|---|---|---|
| ter 13/10 | Hollywood Studios | moderada a alta (UT 6/10) | LL Multi-Pass + Single Rise (avaliar) | sim |
| qua 14/10 | Animal Kingdom | moderada (UT 5/10) | Single Flight of Passage | sim |
| qui 15/10 | EPCOT (Food & Wine) | moderada (UT 5/10), melhor dia | Single Guardians | sim |
| sex 16/10 | descanso e compras | — | — | **não** |
| sáb 17/10 | Magic Kingdom (dia inteiro) | alta (sábado de fall break) | LL Multi-Pass + Singles Tron e Tiana | sim |
| dom 18/10 | descanso + aniversário (California Grill) | — | — | **não** |
| seg 19/10 | Islands of Adventure | moderada a alta (UT 6/10) | sem Express: rope drop + single rider | sim |
| ter 20/10 | Universal Studios Florida | moderada (UT 5/10), melhor dia | sem Express: rope drop Gringotts | sim |
| qua 21/10 | Epic Universe | alta (UT 7/10) | Express Pass comprado | sim |
| qui 22/10 a dom 25/10 | estrada e Miami | — | — | **não** |

Nos dias sem alerta o monitor continua rodando em **modo coleta**: grava o
histórico de todos os 7 parques, só não manda Telegram.

O dia 18/10 tem uma esticada **opcional** ao Wizarding World de dia (um dos 2
dias grátis do ingresso Universal). Ficou fora do `park_days` de propósito, para
não encher o dia de descanso de notificação — se o grupo decidir ir, basta
acrescentar `"2026-10-18": ["Universal Islands Of Adventure"]` (ou USF).

## Por que os dias caem assim

- MNSSHP (festa de Halloween do MK) roda em 13, 15, 16 e 18/10: nesses dias o
  Magic Kingdom fecha às 18h para quem não tem ingresso da festa e não tem os
  fogos tradicionais. **17/10 é o único dia da janela com MK completo**, com
  Happily Ever After — daí o MK cair num sábado apesar de sábado ser o pior dia.
- HHN (Halloween Horror Nights) pega 14 a 18 e 21/10 à noite no USF. A visita ao
  USF em **20/10 é dia limpo**, com horário noturno completo.
- Quinta é em média o dia mais leve nos parques Disney; terça é o mais leve na
  Universal. Seis dos sete dias de parque caem nos dias recomendados.

## Reflexos no monitor

- **Alertas seguem o parque do dia.** Antes desta correção o `park_days` estava
  com a ordem antiga do planejamento e erraria o parque em 8 dos 10 dias.
- **Dinoland e a montanha DINOSAUR fecharam em 2026** — a atração saiu da
  watchlist do Animal Kingdom porque não existe mais na API.
- Atrações que o roteiro prioriza e faltavam entraram na watchlist: Rock 'n'
  Roller Coaster e Alien Swirling Saucers (HS), Kali River Rapids (AK), Buzz
  (MK), Simpsons/E.T./Villain-Con (USF), Kong/Jurassic/Ripsaw/Doom/Hippogriff
  (IOA), Mine-Cart Madness/Hiccup's/Le Cirque Arcanus/Constellation (EU).

## Prazos que o monitor NÃO cobre

Ficam por conta do grupo, no app:

- Multi-Pass do HS: comprar a partir de **10/10**; do MK, a partir de **14/10**.
- Reserva do California Grill: abriu em **19/08/2026** (60 dias antes).
- Confirmar horários oficiais e refurbishments 60 dias antes e de novo na semana
  da viagem.

---

Fontes citadas nos documentos de planejamento: Undercover Tourist (crowd
calendar), Disney Tourist Blog, Thrill Data, Mousehacking, Disney Food Blog.
