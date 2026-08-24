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

## Virar o DNS (sem downtime)

1. Antes de mexer no DNS, teste pelo IP com SNI:
   `curl -k -H "Host: filadisney.premercadosc.com" https://IP_DA_VPS/` —
   deve voltar o HTML novo (o certificado só vem depois do DNS, o `-k` é só
   para este teste).
2. No provedor de DNS, troque o registro `filadisney` de
   `CNAME custom-domains.chatgpt.site` para `CNAME premercadosc.com`
   (ou registro A com o IP da VPS).
3. O Caddy emite o certificado sozinho na primeira visita após a propagação.
4. A página antiga do host externo pode ser desativada depois.

## Depois de mudar o site

`git pull` na VPS basta — o Caddy serve direto da pasta. Só o passo 5 é
necessário de novo se o Caddyfile mudar.
