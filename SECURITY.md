# Segurança

Projeto pessoal, de uma família só. Não há SLA — mas se você encontrar algo,
quero saber.

## Como reportar

Abra um [Security Advisory privado](https://github.com/haohmarusc-glitch/Fila-Disney-/security/advisories/new).
**Não abra issue pública** para falha explorável: o repositório é público e a
instância é uma só.

Sem advisory disponível, mande e-mail para o endereço do perfil do dono do
repositório.

## O que está exposto

O repositório é público **de propósito** — o desenho do acesso é para ser
auditável, não secreto. O que nunca entra no git está no `.gitignore`:

- `.env` — token do bot Telegram, `WEB_API_TOKEN`, `FAMILY_ACCESS_PASSWORD`,
  chave da Google Routes API, URL Push do Uptime Kuma;
- `data/history.db` — o histórico e, principalmente, as tabelas de localização.

Duas superfícies encaram a internet:

| Superfície | Proteção |
|---|---|
| Bot Telegram | só chat autorizado; entrada por `/entrar <senha>` com `hmac.compare_digest`, 5 erros por hora bloqueiam o chat, e a senha certa também não passa durante o bloqueio |
| `api-filadisney.premercadosc.com` | `Authorization: Bearer`, comparação em tempo constante, 10 falhas em 5 min bloqueiam por 5 min, 30 pedidos/min no total |

## O que consideramos vulnerabilidade

Qualquer coisa que permita ler ou escrever dados de outra família, obter posição
GPS de alguém sem o opt-in do `/grupo`, contornar os freios de senha ou de token,
ou executar código no VPS.

## O que não é vulnerabilidade

- O `/health` da API responder sem token: devolve só `{"ok": true, "service": ...}`.
- O desenho do acesso familiar estar visível no código — é intencional.
- Falta de rate limit por IP: atrás do Caddy todo cliente chega com o mesmo
  endereço, então o freio é global de propósito.

## Dados pessoais

Posição GPS só circula com opt-in explícito (`/grupo on`), nunca sai como lat/lon
numa mensagem, e as tabelas de localização expiram em 7 dias. `/sair` e
`/revogar` apagam posição, nome e compartilhamento do chat na hora.
