#!/usr/bin/env bash
#
# Tira o WEB_API_TOKEN do navegador. Roda na VPS, de dentro de ~/Fila-Disney-.
#
#   ./scripts/fechar_token.sh --conferir   mostra o que mudaria, não aplica
#   ./scripts/fechar_token.sh              aplica
#
# Faz, nesta ordem: bloco do Caddy com basic_auth + injeção do header, remoção
# do site/config.js, troca do token nos dois .env e restart dos containers.
#
# O Caddyfile é do Premercado. O passo que mexe nele tem cópia de segurança e
# passa por `caddy validate` antes do reload — config recusada derruba o
# premercadosc.com junto, e é por isso que existe o --conferir.
set -euo pipefail

PROJETO="${PROJETO:-$HOME/Fila-Disney-}"
PREMERCADO="${PREMERCADO:-/opt/premercado}"
HOST="filadisney.premercadosc.com"
CADDYFILE="$PREMERCADO/Caddyfile"
CONFERIR=0
[[ "${1:-}" == "--conferir" ]] && CONFERIR=1

erro() { echo "ERRO: $*" >&2; exit 1; }
passo() { echo; echo "== $*"; }

cd "$PROJETO" 2>/dev/null || erro "não achei $PROJETO"
[[ -f "$CADDYFILE" ]] || erro "não achei $CADDYFILE"
command -v docker >/dev/null || erro "docker não está no PATH"

dc_pre() { docker compose --project-directory "$PREMERCADO" -f "$PREMERCADO/docker-compose.yml" "$@"; }

# ---------------------------------------------------------------- 1. Caddyfile
passo "1/5 bloco do Caddy"

if grep -qE "^[[:space:]]*basic_auth[[:space:]]*\{" "$CADDYFILE"; then
    DIRETIVA=basic_auth
elif grep -qE "^[[:space:]]*basicauth[[:space:]]*\{" "$CADDYFILE"; then
    DIRETIVA=basicauth
else
    # `basic_auth` é Caddy 2.8+; antes disso chama-se `basicauth`. Perguntar à
    # versão é mais confiável que adivinhar — nome errado faz o Caddy recusar a
    # config inteira, e aí o premercadosc.com cai junto.
    VERSAO="$(dc_pre exec -T caddy caddy version 2>/dev/null | head -1 || true)"
    echo "   caddy: ${VERSAO:-versão desconhecida}"
    if [[ "$VERSAO" =~ v2\.([0-9]+) ]] && (( ${BASH_REMATCH[1]} < 8 )); then
        DIRETIVA=basicauth
    else
        DIRETIVA=basic_auth
    fi
fi
echo "   diretiva: $DIRETIVA"

HASH_EXISTENTE="$(grep -E '^[[:space:]]+familia[[:space:]]+\$2[aby]\$' "$CADDYFILE" | head -1 | awk '{print $2}' || true)"
if [[ -n "${SENHA_HASH:-}" ]]; then
    HASH="$SENHA_HASH"
elif [[ -n "$HASH_EXISTENTE" ]]; then
    # Já tem senha configurada: reaproveita o hash em vez de pedir de novo.
    HASH="$HASH_EXISTENTE"
    echo "   senha: reaproveitando o hash que já está no Caddyfile"
elif (( CONFERIR )); then
    # Conferência não aplica nada, então não tem por que pedir a senha — pedir
    # aqui obriga a digitá-la duas vezes, uma para ver o diff e outra para
    # valer, gerando dois hashes diferentes da mesma senha.
    HASH='$2a$14$<hash da senha que o modo de aplicar vai pedir>'
    echo "   senha: será pedida ao aplicar (aqui entra um marcador no lugar do hash)"
else
    read -rsp "   senha da família (a que todo mundo vai digitar no site): " SENHA; echo
    [[ -n "$SENHA" ]] || erro "senha vazia"
    HASH="$(dc_pre exec -T caddy caddy hash-password --plaintext "$SENHA" | tr -d '\r\n')"
    unset SENHA
    [[ "$HASH" == \$2* ]] || erro "hash-password não devolveu um hash bcrypt: $HASH"
fi

BLOCO="$(mktemp)"; NOVO="$(mktemp)"
trap 'rm -f "$BLOCO" "$NOVO"' EXIT

# Montar o candidato é função porque pode ser preciso montar duas vezes: se a
# versão do Caddy não for legível e a diretiva sair errada, o `validate` lá
# embaixo recusa e a segunda tentativa usa o outro nome.
montar() {
    cat > "$BLOCO" <<EOF
$HOST {
    encode zstd gzip

    # A senha protege a página inteira, inclusive o /api/*. É ela que substitui
    # o token que ficava no JavaScript.
    $1 {
        familia $HASH
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
    python3 "$PROJETO/scripts/caddy_bloco.py" "$CADDYFILE" "$HOST" "$BLOCO" > "$NOVO"
}
montar "$DIRETIVA"

if diff -q "$CADDYFILE" "$NOVO" >/dev/null; then
    echo "   Caddyfile já está como deveria"
else
    diff -u "$CADDYFILE" "$NOVO" | sed 's/^/   /' || true
fi

# --------------------------------------------------------------- 2. override
passo "2/5 override do compose (passa o token ao Caddy)"
OVERRIDE="$PREMERCADO/docker-compose.override.yml"
if [[ -f "$OVERRIDE" ]] && grep -q WEB_API_TOKEN "$OVERRIDE"; then
    echo "   já existe"
    PRECISA_OVERRIDE=0
else
    echo "   vai criar $OVERRIDE"
    PRECISA_OVERRIDE=1
fi

# ------------------------------------------------------------------ 3. token
passo "3/5 token novo"
TOKEN_NOVO="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "   gerado (não será impresso); vai para $PROJETO/.env e $PREMERCADO/.env"

# --------------------------------------------------------------- 4. config.js
passo "4/5 site/config.js"
if [[ -f "$PROJETO/site/config.js" ]]; then
    echo "   existe e será apagado (é o arquivo que servia o token à internet)"
else
    echo "   não existe"
fi

if (( CONFERIR )); then
    passo "--conferir: nada foi alterado"
    exit 0
fi

# =========================================================== aplicando de fato
passo "aplicando"

BACKUP="$CADDYFILE.bak-$(date +%Y%m%d-%H%M%S)"
cp "$CADDYFILE" "$BACKUP"
echo "   backup: $BACKUP"
cp "$NOVO" "$CADDYFILE"

if (( PRECISA_OVERRIDE )); then
    cat > "$OVERRIDE" <<EOF
services:
  caddy:
    volumes:
      - $PROJETO/site:/srv/filadisney:ro
    environment:
      - WEB_API_TOKEN=\${WEB_API_TOKEN}
EOF
fi

# O token vai para os dois .env antes de qualquer restart: a API e o Caddy
# precisam subir já com o mesmo valor, senão o site fica 401 no meio do caminho.
trocar_env() {
    python3 - "$1" "$TOKEN_NOVO" <<'PY'
import pathlib, sys
caminho, token = pathlib.Path(sys.argv[1]), sys.argv[2]
linhas = caminho.read_text(encoding="utf-8").splitlines() if caminho.exists() else []
saida, achou = [], False
for linha in linhas:
    if linha.startswith("WEB_API_TOKEN="):
        saida.append(f"WEB_API_TOKEN={token}")
        achou = True
    else:
        saida.append(linha)
if not achou:
    saida.append(f"WEB_API_TOKEN={token}")
caminho.write_text("\n".join(saida) + "\n", encoding="utf-8")
PY
}
trocar_env "$PROJETO/.env"
trocar_env "$PREMERCADO/.env"
chmod 600 "$PROJETO/.env" "$PREMERCADO/.env" 2>/dev/null || true
echo "   token trocado nos dois .env"

rm -f "$PROJETO/site/config.js"

# Valida ANTES de recarregar. Config recusada com o Caddy já recarregando
# levaria o premercadosc.com junto.
validar() { dc_pre exec -T caddy caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile; }

if ! validar; then
    # Quase sempre é o nome da diretiva: `basic_auth` no Caddy 2.7, ou
    # `basicauth` no 2.8+. Tenta o outro antes de desistir.
    OUTRA=basicauth; [[ "$DIRETIVA" == basicauth ]] && OUTRA=basic_auth
    echo "   validate recusou com '$DIRETIVA'; tentando '$OUTRA'"
    montar "$OUTRA"
    cp "$NOVO" "$CADDYFILE"
    if ! validar; then
        cp "$BACKUP" "$CADDYFILE"
        erro "caddy validate recusou as duas formas; Caddyfile restaurado de $BACKUP (o Caddy em execução não foi tocado)"
    fi
fi

passo "5/5 subindo"
docker compose up -d --build fila-disney-api
dc_pre up -d caddy

echo
echo "== conferindo"

# Bate em localhost com o Host na mão, não no nome público: de dentro da VPS o
# DNS pode não voltar para a própria máquina, e aí o curl falha sem chegar no
# Caddy — devolvendo 000, que não diz nada sobre o site. O `-k` é porque o
# certificado é do nome público, não de "localhost".
codigo_de() {
    curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
        -H "Host: $1" "https://localhost${2}" 2>/dev/null || echo 000
}

# O container acabou de subir; o Caddy leva alguns segundos para ouvir.
for _ in $(seq 12); do
    [[ "$(codigo_de "$HOST" /)" != "000" ]] && break
    sleep 2
done

CODIGO="$(codigo_de "$HOST" /config.js)"
echo "   GET /config.js -> $CODIGO   (401 = atrás da senha, 404 = apagado; 200 seria o defeito de volta)"
CODIGO="$(codigo_de "$HOST" /)"
echo "   GET /          -> $CODIGO   (401 esperado, agora que a senha existe)"
# O Premercado divide este Caddy: se ele parou de responder, a suspeita é esta
# mudança. Só vale checar o hostname que estiver mesmo no Caddyfile — inventar
# um nome daria 000 sempre, e um alarme falso aqui manda desfazer o que deu
# certo.
VIZINHO="$(grep -oE '^[a-z0-9.-]*premercadosc\.com' "$CADDYFILE" | grep -v "^$HOST\$" | head -1 || true)"
if [[ -n "$VIZINHO" ]]; then
    CODIGO="$(codigo_de "$VIZINHO" /)"
    echo "   $VIZINHO -> $CODIGO   (qualquer código serve; 000 é o Caddy sem responder)"
    if [[ "$CODIGO" == "000" ]]; then
        echo
        echo "!! $VIZINHO parou de responder, e ele divide este Caddy. Para voltar atrás:"
        echo "   cp $BACKUP $CADDYFILE && cd $PREMERCADO && docker compose up -d caddy"
        exit 1
    fi
fi

echo
echo "Abra https://$HOST no navegador, entre com usuário 'familia' e a senha,"
echo "e confira que as filas aparecem. Se algo falhar: cp $BACKUP $CADDYFILE"
