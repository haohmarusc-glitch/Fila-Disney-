/* Roda o site/app.js fora do navegador, com um DOM mínimo, para que o
 * test_site_js.py possa afirmar o que a TELA mostra — não o que o código diz.
 *
 * Existe porque dois defeitos chegaram ao celular passando por 469 testes
 * verdes: a aba "Melhores agora" em branco com o parque fechado, e a vigia
 * anunciando "fila agora 0 min" numa atração que não estava funcionando.
 * Nenhum teste de Python alcançava essas linhas — elas são JavaScript.
 *
 * Recebe o caso como JSON em argv[2] e imprime o texto renderizado de cada
 * painel. Sem dependência: só `node`, que o runner do CI já tem.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const caso = JSON.parse(process.argv[2]);

function criar(tag) {
  return {
    tag,
    className: "",
    textContent: "",
    style: {},
    dataset: {},
    href: "",
    target: "",
    rel: "",
    children: [],
    appendChild(filho) { this.children.push(filho); return filho; },
    replaceChildren(...filhos) { this.children = filhos; },
    get childElementCount() { return this.children.length; },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    scrollIntoView() {},
    querySelectorAll() { return []; },
  };
}

const elementos = {};
globalThis.document = {
  createElement: criar,
  getElementById(id) {
    if (!elementos[id]) elementos[id] = criar("div");
    return elementos[id];
  },
  querySelectorAll() { return []; },
};
globalThis.window = { FILA_CONFIG: { token: "token-de-teste" } };
globalThis.localStorage = { getItem: () => null, setItem() {} };
// O Node 22 já traz um `navigator` global e ele só tem getter — atribuir
// direto joga TypeError. defineProperty sobrescreve.
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  writable: true,
  value: {
    geolocation: {
      watchPosition(ok) {
        ok({ coords: { latitude: 28.3575, longitude: -81.5906 } });
      },
    },
  },
});

globalThis.fetch = async (url) => {
  const rota = String(url).replace(/^\/api/, "").split("?")[0];
  const corpo = caso.respostas[rota];
  if (corpo === undefined) {
    throw new Error(`o teste não previu a rota ${rota}`);
  }
  return { ok: true, json: async () => corpo };
};

vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, "..", "site", "app.js"), "utf8"));

function textoDe(no) {
  if (!no) return "";
  return [no.textContent || "", ...no.children.map(textoDe)]
    .filter(Boolean).join(" | ");
}

(async () => {
  if (caso.aba) trocarAba(caso.aba);
  if (caso.gps) iniciarGPS();
  // As cargas são assíncronas e ninguém devolve promessa aqui; um tick de
  // folga basta porque o fetch é resolvido na hora pelo stub.
  await new Promise((resolve) => setTimeout(resolve, 50));
  console.log(JSON.stringify({
    perto: textoDe(elementos["perto-conteudo"]),
    vigias: textoDe(elementos["vigias-conteudo"]),
    subtitulo: elementos["subtitulo"] ? elementos["subtitulo"].textContent : "",
  }));
  process.exit(0); // o app.js deixa um setInterval de pé, que seguraria o node
})();
