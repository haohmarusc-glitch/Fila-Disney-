# O site (filadisney.premercadosc.com)

O frontend mora em `site/` neste repositório: três arquivos estáticos
(`index.html`, `styles.css`, `app.js`) mais o `config.js` local, servidos pelo
Caddy do Premercado. Sem framework e sem build — deploy é `git pull`.

Histórico: até 24/08/2026 o site era uma página hospedada fora
(`custom-domains.chatgpt.site`, via CNAME). Foi reconstruído aqui para ficar
versionado e sob o mesmo Caddy da VPS.

## Como funciona

- **Mesmo domínio para página e API.** O Caddy serve os estáticos e repassa
  `/api/*` ao container `fila-disney-api` — sem CORS, sem segundo hostname.
  O `api-filadisney.premercadosc.com` continua existindo e serve o mesmo
  container; pode ser aposentado quando nada mais o usar.
- **Abas**: "Melhores agora" (GPS do navegador → `/api/perto`) e "Vigias"
  (`/api/vigias`, painel somente-leitura — criar/cancelar é no Telegram).
- **Token**: `site/config.js`, copiado do `config.example.js`, com o mesmo
  `WEB_API_TOKEN` do `.env`. Não é versionado; fica visível a quem abrir o
  site, o que é o desenho — a página é da família, atrás do token.
- Atualização automática a cada 60 s (o passo do cache da API), atribuição
  "Powered by Queue-Times.com" no rodapé (regra 2), fila ausente vira "—",
  nunca 0 (regra 15).

## Instalação na VPS (uma vez)

```bash
# 1. o site chega com o git pull normal do Fila-Disney-
cd ~/Fila-Disney- && git pull

# 2. token do site (uma vez; refazer se o WEB_API_TOKEN mudar)
cd site && cp config.example.js config.js
sed -i "s/COLE_AQUI_O_WEB_API_TOKEN/$(grep ^WEB_API_TOKEN ../.env | cut -d= -f2)/" config.js

# 3. bloco no Caddyfile do Premercado (uma vez)
cat >> /opt/premercado/Caddyfile <<'EOF'

filadisney.premercadosc.com {
    encode zstd gzip
    handle /api/* {
        uri strip_prefix /api
        reverse_proxy fila-disney-api:8080
    }
    root * /srv/filadisney
    file_server
}
EOF

# 4. monta a pasta do site no container do Caddy (uma vez)
cat > /opt/premercado/docker-compose.override.yml <<'EOF'
services:
  caddy:
    volumes:
      - /root/Fila-Disney-/site:/srv/filadisney:ro
EOF

# 5. aplica
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
   (`{"ok": true, ...}`), que é a única rota sem token.
7. Sobraram do host antigo dois TXT — `_cf-custom-hostname.filadisney` e
   `_openai-site-verification.filadisney`. Não atrapalham; apagar só depois
   de confirmar a página, para não misturar duas mudanças.

## Depois de mudar o site

`git pull` na VPS basta — o Caddy serve direto da pasta. Só o passo 5 é
necessário de novo se o Caddyfile mudar.
