/* Opcional. O site NÃO precisa mais de token: quem injeta o header
 * Authorization é o Caddy, no repasse de /api/*, lendo o WEB_API_TOKEN do
 * ambiente. Ver docs/SITE.md.
 *
 * A versão anterior deste arquivo mandava colar o token aqui, e o Caddy serve
 * este arquivo como estático — qualquer pessoa na internet abria /config.js e
 * levava a credencial. Se o seu config.js ainda tem `token`, apague o arquivo
 * e troque o WEB_API_TOKEN: considere o antigo comprometido.
 *
 * Copie para config.js só se precisar apontar para outra API (um túnel de
 * teste, por exemplo). Sem config.js o site usa /api no mesmo domínio.
 */
window.FILA_CONFIG = {
  apiBase: "/api",
};
