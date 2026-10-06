# Roteiro dos Parques — Flórida 2026

Fonte: “Calendário mestre FIRME v11”, enviado pela família em 06/10/2026, que
substitui o cronograma de 12/09. Este roteiro, `site/roteiro.json` e
`watchlist.json` devem permanecer sincronizados (regra 17). Detalhes do
planejamento anterior são mantidos quando compatíveis.

## Agenda

| Data | Parque / atividade | Alertas de parque |
|---|---|---|
| seg 12/10 | Voo e chegada em Orlando; Florida Mall | não |
| ter 13/10 | Animal Kingdom | sim |
| qua 14/10 | Hollywood Studios | sim |
| qui 15/10 | EPCOT, Food & Wine | sim |
| sex 16/10 | Descanso em Orlando, compras e NBA no Kia Center | não |
| sáb 17/10 | Magic Kingdom, dia inteiro | sim |
| dom 18/10 | Islands of Adventure, dia leve; aniversário à noite | sim, Islands |
| seg 19/10 | Universal Studios Florida, dia inteiro | sim, USF |
| ter 20/10 | Epic Universe | sim |
| qua 21/10 | Islands of Adventure, dia inteiro | sim, Islands |
| qui 22/10 | Estrada para Miami, Sawgrass | não |
| sex 23/10 | Compras em Miami, Design District | não |
| sáb 24/10 | Ocean Drive e estrada de volta, Florida Mall no caminho | não |
| dom 25/10 | Voo de volta, saindo de Orlando | não |

## O que o v11 mudou em relação ao cronograma de 12/09

- 13 e 14/10 trocaram: o Animal Kingdom passou para terça e o Hollywood Studios
  para quarta. Isso move também a compra do fura-fila de cada um.
- 19/10 deixou de ser Park-to-Park: é dia só de Universal Studios Florida, e a
  chave `parques` saiu do cartão do site.
- 20 e 21/10 trocaram: Epic Universe na terça, Islands of Adventure na quarta.
- O Express Pass passou do Epic de 21/10 para o Epic de 20/10.
- Transporte: 2 Jeeps da Alamo, não uma SUV. Os ingressos do NBA já estão
  comprados, para 8 pessoas, e a família decidiu não fazer o HHN.

## Reflexos no monitor e no site

- 12, 16 e 22–25/10 ficam fora de `park_days`; a coleta dos sete parques continua.
- A viagem continua de 12 a 25/10/2026, no fuso `America/New_York`.
- Nenhum dia do v11 tem dois parques, então nenhum cartão do site usa a chave
  `parques`.
- O detalhamento de atrações já existente foi preservado nos dias que não mudaram.

## Horários de referência

Conferidos em 06/10/2026 por agregadores (ThemeParks.wiki, wdwmagic, standbyboard,
deeparrival), **não** no site oficial — o calendário da Disney respondeu HTTP 500 e
a página da Universal só carrega com JS. Reconferir no app na véspera. Estes
números não são fixados em lugar nenhum do código, de propósito (regra 19).

| Data | Parque | Horário (ET) | Observação |
|---|---|---|---|
| 13/10 | Animal Kingdom | 08:00–18:00 | fecha cedo; festa de Halloween no MK nesta noite |
| 14/10 | Hollywood Studios | 09:00–21:00 | Fantasmic 20:00 e 21:30 |
| 15/10 | EPCOT | 09:00–21:00 | Luminous 21:00 |
| 17/10 | Magic Kingdom | 08:00–23:00 | **sem festa** nesta data; Happily Ever After 21:00 |
| 18/10 | Islands of Adventure | 09:00–20:00 | fogos da festa do MK às 22:00, vistos do California Grill |
| 19/10 | Universal Studios | **09:00–17:00** | HHN Premium Scream Night das 18:30 às 02:00 |
| 20/10 | Epic Universe | 10:00–20:00 | sem evento; o HHN não acontece aqui |
| 21/10 | Islands of Adventure | 09:00–20:00 | HHN no USF nesta noite, sem efeito no Islands |

### O aviso de fechamento do bot não vale para o USF em 19/10

O horário de operação é medido pelo histórico de 30 dias (`horario_operacao`),
não cravado. No Universal Studios isso dá `(0, 23)`: o HHN opera as atrações até
as 02h, a API continua publicando fila durante o evento, e a função devolve a
primeira e a última hora que passam do corte, mesmo sem serem contíguas. Medido
em 05/10/2026, as horas que passam são `[0, 1, 10, 11, …, 23]`.

Consequência: no `/status` do Universal Studios a linha “Opera por volta de…”
sai errada e o aviso “⏳ não cabe antes de fechar” nunca dispara. Nos outros seis
parques o número medido bate com o portão, porque `cabe_antes_de_fechar` usa
`fechamento:59:59`.

Não é consertável a partir do `is_open`: o parque **está** aberto às 22h; o que
muda é o tipo de ingresso, e a Queue-Times não publica isso. Um lembrete em
19/10 às 08h avisa a família. O conserto de verdade seria um arquivo versionado
de horários oficiais, no mesmo espírito do `coords.json` e do `duracoes.json` —
assunto para depois da viagem.

## Reformas no período

- **Jurassic Park River Adventure** (Islands) fechado desde 05/01/2026, volta em
  19–20/11. Está na watchlist e vai aparecer como fechado nos dias 18 e 21/10.
- **Skull Island: Reign of Kong** (Islands) operando com o final modificado.
- Walt Disney World Railroad (MK) fechado de 28/09 a 29/10.
- Tiana's Bayou Adventure (MK) fecha em 02/11, então está aberta em 17/10.
- Rock 'n' Roller Coaster (HS) agora é “Starring The Muppets”; o nome continua
  casando com a watchlist pela normalização.

## Reservas e horários

Conferir voos, reservas, jogo de basquete, horários oficiais dos parques e fogos
nos comprovantes e aplicativos. As fotos detalhadas permitem transcrever os horários planejados; eles não
substituem os comprovantes nem confirmam disponibilidade.

- Fura-fila (opção B): Single do Flight of Passage em 13/10, Multi-Pass do
  Hollywood em 14/10, Single do Guardians em 15/10, Multi-Pass mais Single do
  TRON em 17/10 (a Tiana entra no Multi) e Express do Epic em 20/10. A compra
  abre 3 dias antes, de manhã, e tem lembrete armado para cada uma.
- 16/10: Magic × Heat às 19h, ingressos já comprados para 8 pessoas e
  estacionamento resolvido. Pré-venda dos 2 iPhone Duo às 08h (5h PT), para
  retirada em 23/10, conforme o plano da família.
- 17/10: entrada na abertura oficial, sem madrugada após o NBA.
- 18/10: sair do Islands por volta das 18h; California Grill às 19h30, mesa para 8.
- 19/10: rope drop no Gringotts; sair às 17h, quando o parque fecha por causa do HHN.
- 20/10: Express Pass previsto, compra ainda não feita.
- 22/10: saída de Kissimmee às 07h; check-in em North Miami Beach às 16h.
- 23/10: Apple Aventura e Design District.
- 24/10: checkout até 11h, Ocean Drive e retorno à casa em Kissimmee, com
  parada no Florida Mall.
- 25/10: CM 435 sai de MCO às 08h01, conexão de 4h42 no Panamá; CM 423
  chega em Florianópolis às 00h20 de 26/10.

Ainda em aberto no cronograma: Lightning Lane, Express Pass e seguro Visa
Infinite. Pré-venda e retirada de produtos devem ser confirmadas com a loja.
