# O site (filadisney.premercadosc.com)

O frontend mora em `site/` neste repositório: três arquivos estáticos
(`index.html`, `styles.css`, `app.js`), servidos pelo Caddy do Premercado.
Sem framework e sem build — deploy é `git pull`.

Histórico: até 24/08/2026 o site era uma página hospedada fora
(`custom-domains.chatgpt.site`, via CNAME). Foi reconstruído aqui para ficar
versionado e sob o mesmo Caddy da VPS.

## Como funciona

- **Mesmo domínio para página e API.** O Caddy serve os estáticos e repassa
  `/api/*` ao container `fila-disney-api` — sem CORS, sem segundo hostname.
  O `api-filadisney.premercadosc.com` continua existindo e serve o mesmo
  container; pode ser aposentado quando nada mais o usar.
- **Abas**: "Melhores agora" (GPS do navegador → `/api/perto`), "Parques"
  (escolhe o parque sem GPS e roda os comandos), "Roteiro" (os 14 dias com as
  filas ao vivo) e "Vigias" (`/api/vigias`, painel somente-leitura —
  criar/cancelar é no Telegram).
- **Aba Parques**: mostra o parque em três blocos — a watchlist, as outras
  atrações com fila, e "Shows e sem fila". Este último sai **sem número**: a
  Queue-Times publica `wait_time` 0 para show, trilha e marco, e escrever
  "0 min" diria que não há espera onde não há medição. Quem separa é o
  histórico (`atracoes_sem_fila_medida`), o mesmo detector do `/menores`.
  Abaixo, os 11 comandos do Telegram como botões; o texto que aparece é o
  mesmo que chega no chat, convertido por uma lista fechada de tags — nunca
  por `innerHTML`.
- **Token**: NÃO fica no navegador. O Caddy injeta o header `Authorization`
  no repasse de `/api/*`, lendo o `WEB_API_TOKEN` do ambiente. Quem protege a
  página é a senha do próprio Caddy (`basic_auth`).

  Até 25/08/2026 o token era colado em `site/config.js` e o texto aqui dizia
  que "fica visível a quem abrir o site, o que é o desenho". Estava errado, e
  de um jeito que o `.env.example` já contradizia: o Caddy serve o `config.js`
  como arquivo estático, então **qualquer pessoa na internet** abria
  `https://filadisney.premercadosc.com/config.js` e levava a credencial. Não
  era "visível para a família" — era pública, e a barreira do token não
  existia.
- Atualização automática a cada 60 s (o passo do cache da API), atribuição
  "Powered by Queue-Times.com" no rodapé (regra 2), fila ausente vira "—",
  nunca 0 (regra 15).

## Instalação na VPS (uma vez)

```bash
# 1. o site chega com o git pull normal do Fila-Disney-
cd ~/Fila-Disney- && git pull

# 2. o site NÃO tem mais config.js com token. Se existir um de antes, apague:
rm -f ~/Fila-Disney-/site/config.js

# 3. senha da página (uma vez). Guarde a senha; o hash é o que vai no Caddyfile
caddy hash-password --plaintext 'SENHA_DA_FAMILIA'

# 4. bloco no Caddyfile do Premercado (uma vez)
#    Troque COLE_O_HASH pela saída do passo 3.
cat >> /opt/premercado/Caddyfile <<'EOF'

filadisney.premercadosc.com {
    encode zstd gzip

    # A senha protege a página inteira, inclusive o /api/*. É ela que substitui
    # o token que ficava no JavaScript.
    basic_auth {
        familia COLE_O_HASH
    }

    handle /api/* {
        uri strip_prefix /api
        # Quem injeta o token é AQUI, no servidor. O navegador nunca o vê.
        reverse_proxy fila-disney-api:8080 {
            header_up Authorization "Bearer {env.WEB_API_TOKEN}"
        }
    }
    root * /srv/filadisney
    file_server
}
EOF

# 5. monta a pasta e passa o token ao container do Caddy (uma vez)
cat > /opt/premercado/docker-compose.override.yml <<'EOF'
services:
  caddy:
    volumes:
      - /root/Fila-Disney-/site:/srv/filadisney:ro
    environment:
      - WEB_API_TOKEN=${WEB_API_TOKEN}
EOF

# 6. o WEB_API_TOKEN precisa existir no .env DO PREMERCADO, que é onde este
#    docker compose lê as variáveis:
grep ^WEB_API_TOKEN ~/Fila-Disney-/.env >> /opt/premercado/.env

# 7. aplica
cd /opt/premercado && docker compose up -d caddy
```

`basic_auth` é a diretiva do Caddy 2.8+; em versões anteriores chama-se
`basicauth`, com a mesma sintaxe. Confira com `docker compose exec caddy caddy
version` antes de recarregar — nome errado faz o Caddy recusar a config
inteira, e aí o `premercadosc.com` cai junto.

## Trocar o token (obrigatório uma vez)

O token antigo esteve público em `/config.js` enquanto o site esteve no ar.
Trate-o como comprometido:

```bash
# 1. gera um novo
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. troca nos DOIS .env (o do projeto e o do Premercado, que o Caddy lê)
#    e sobe os containers de novo
cd ~/Fila-Disney- && docker compose up -d --build fila-disney-api
cd /opt/premercado && docker compose up -d caddy
```

O override existe para não editar o `docker-compose.yml` do Premercado, que
tem drift próprio em relação ao GitHub. O Caddyfile da VPS também divergiu do
repositório (o bloco `api-filadisney` só existe lá) — por isso este runbook é
de comandos, não um PR no Premercado.

## A virada do DNS (feita em 24/08/2026)

Está escrito aqui porque cada tropeço custou tempo e o mesmo caminho vale
para qualquer subdomínio novo no Cloudflare.

1. Antes de mexer no DNS, teste pelo IP com SNI:
   `curl -k -H "Host: filadisney.premercadosc.com" https://IP_DA_VPS/` — ou,
   de dentro da VPS, `curl -si -H "Host: ..." http://localhost/`, que deve
   voltar `308` para o HTTPS. Isso prova que o Caddy reconhece o hostname
   antes de qualquer questão de certificado.
2. No Cloudflare, o registro era `CNAME filadisney → custom-domains.chatgpt.site`.
   Trocado por **A → IP da VPS**, com Proxy status **DNS only** (nuvem cinza).
   Proxy ligado (nuvem laranja) quebra o ACME e o Caddy nunca emite nada.
3. Confira que não sobrou `AAAA`: `dig +short AAAA filadisney.premercadosc.com`
   tem que vir vazio. O Let's Encrypt **prefere IPv6**, então um `AAAA` órfão
   apontando para o host antigo derruba a validação mesmo com o `A` correto —
   o erro aparece como `Invalid response from http://.../.well-known/... : 400`
   citando um endereço `2606:4700::` (Cloudflare) em vez do IP da VPS.
4. O Caddy não tenta de novo na hora. Depois de falhar ele entra em backoff
   crescente (`retrying_in` vai a 600s), então logo após a propagação o site
   ainda dá `TLS connect error: tlsv1 alert internal error` — que é "não tenho
   certificado para este nome", não erro de configuração. Force com
   `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile` e
   dê ~30s antes de testar; aqui o `curl` foi 26 segundos cedo demais e
   pareceu falha.
5. Depois de várias falhas o CertMagic migra sozinho para
   `acme-staging-v02` — proteção contra rate limit, não configuração errada.
   Ele volta à produção quando a validação passa. O sinal de fim é
   `certificate obtained successfully` com `issuer` em `acme-v02` **sem** o
   `-staging-`; as linhas `served key authentication` logo acima são os
   validadores do Let's Encrypt chegando na VPS.
6. Verificação final, as duas metades:
   `curl -sS -o /dev/null -D - https://filadisney.premercadosc.com/` (200 e
   `server: Caddy`) e `curl -sS https://filadisney.premercadosc.com/api/health`
   (`{"ok": true, ...}`) — a única rota da API que não exige token, mas que
   passa pelo `basic_auth` como o resto do site.
7. Sobraram do host antigo dois TXT — `_cf-custom-hostname.filadisney` e
   `_openai-site-verification.filadisney`. Não atrapalham; apagar só depois
   de confirmar a página, para não misturar duas mudanças.

## Depois de mudar o site

`git pull` na VPS basta — o Caddy serve direto da pasta. Só o passo 7 é
necessário de novo se o Caddyfile mudar.
