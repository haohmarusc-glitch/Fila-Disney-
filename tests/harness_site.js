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
    ouvintes: {},
    addEventListener(tipo, fn) { this.ouvintes[tipo] = fn; },
    click() { if (this.ouvintes.click) this.ouvintes.click(); },
    scrollIntoView() {},
    querySelectorAll() { return []; },
  };
}

/* Acha o primeiro nó cujo texto casa — é assim que o teste clica num botão
 * pelo rótulo que o usuário vê, em vez de por um seletor interno. */
function acharPorTexto(no, alvo) {
  if (!no) return null;
  if (no.textContent === alvo) return no;
  for (const filho of no.children) {
    const achado = acharPorTexto(filho, alvo);
    if (achado) return achado;
  }
  return null;
}

/* Todas as classes usadas na árvore — é como o teste afirma que "fechada" não
 * saiu pintada de verde, já que a cor mora na classe e não no texto. */
function classesDe(no) {
  if (!no) return [];
  return [no.className, ...no.children.flatMap(classesDe)].filter(Boolean);
}

/* Serializa a árvore com as tags, para o teste ver que <b> virou <b> e que
 * <script> não virou nada. */
/* Todos os href da árvore — para o teste afirmar que o link do mapa aponta
 * para o lugar certo, e que muda de forma quando há GPS. */
function linksDe(no) {
  if (!no) return [];
  return [no.href, ...no.children.flatMap(linksDe)].filter(Boolean);
}

function estruturaDe(no) {
  if (!no) return "";
  const dentro = no.children.map(estruturaDe).join("");
  const proprio = no.textContent || "";
  if (!no.tag || no.tag === "div" || no.tag === "span") return proprio + dentro;
  const attrs = no.href ? ` href="${no.href}"` : "";
  return `<${no.tag}${attrs}>${proprio}${dentro}</${no.tag}>`;
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
    // O app.js chama iniciarGPS() sozinho ao carregar, então quem decide se
    // há posição é o stub, não uma chamada extra do teste: `gps: true`
    // simula permissão concedida, a ausência simula permissão negada — que é
    // o estado de quem abre a aba Parques em casa.
    geolocation: {
      watchPosition(ok, falhou) {
        if (caso.gps) {
          ok({ coords: { latitude: 28.3575, longitude: -81.5906 } });
        } else if (falhou) {
          falhou({ code: 1, message: "permissão negada (teste)" });
        }
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
  if (caso.telegram !== undefined) {
    // Modo direto: só o conversor de HTML do Telegram, sem carregar tela.
    const bloco = doTelegram(caso.telegram);
    console.log(JSON.stringify({
      texto: textoDe(bloco).split(" | ").join(""),
      estrutura: bloco.children.map(estruturaDe).join(""),
    }));
    process.exit(0);
  }
  if (caso.aba) trocarAba(caso.aba);
  // As cargas são assíncronas e ninguém devolve promessa aqui; um tick de
  // folga basta porque o fetch é resolvido na hora pelo stub.
  await new Promise((resolve) => setTimeout(resolve, 50));
  if (caso.clicar) {
    const botao = acharPorTexto(elementos[`${caso.aba}-conteudo`], caso.clicar);
    if (!botao) throw new Error(`não achei o botão "${caso.clicar}" na tela`);
    botao.click();
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  console.log(JSON.stringify({
    perto: textoDe(elementos["perto-conteudo"]),
    parques: textoDe(elementos["parques-conteudo"]),
    classes: classesDe(elementos["parques-conteudo"]),
    links: linksDe(elementos["parques-conteudo"]).concat(
      linksDe(elementos["perto-conteudo"])),
    vigias: textoDe(elementos["vigias-conteudo"]),
    subtitulo: elementos["subtitulo"] ? elementos["subtitulo"].textContent : "",
  }));
  process.exit(0); // o app.js deixa um setInterval de pé, que seguraria o node
})();
