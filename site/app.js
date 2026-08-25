/* Fila Disney — painel móvel.
 *
 * Fala com a API pelo MESMO domínio (`/api/*`, repassado pelo Caddy ao
 * container fila-disney-api): sem CORS, sem segundo hostname. O token vem do
 * config.js, que NÃO é versionado — o repositório publica só o exemplo.
 *
 * Somente leitura, como a API: criar e cancelar vigia é no Telegram, porque o
 * alerta precisa de um chat de destino e o site tem um token só para todos.
 */
"use strict";

const CFG = window.FILA_CONFIG || {};
const API = (CFG.apiBase || "/api").replace(/\/$/, "");
const ATUALIZA_MS = 60_000; // mesmo passo do cache da API; menos que isso é ruído

const el = (id) => document.getElementById(id);
let posicao = null;      // [lat, lon] da última leitura de GPS
let abaAtiva = "perto";
let timer = null;

/* ---------------------------------------------------------------- comum */

async function api(caminho) {
  const resp = await fetch(`${API}${caminho}`, {
    headers: { Authorization: `Bearer ${CFG.token || ""}` },
  });
  if (!resp.ok) {
    let corpo = null;
    try { corpo = await resp.json(); } catch { /* corpo não-JSON: mantém null */ }
    const erro = new Error((corpo && corpo.error) || `HTTP ${resp.status}`);
    erro.status = resp.status;
    throw erro;
  }
  return resp.json();
}

function mensagemDeErro(erro) {
  if (erro.status === 401) {
    return "Token inválido. Confira o config.js do site.";
  }
  if (erro.status === 429) {
    return "Muitos pedidos agora — a página tenta de novo sozinha em instantes.";
  }
  if (erro.status === 400) {
    return erro.message; // "localização fora dos parques monitorados"
  }
  return "Não consegui falar com a API agora. Tentando de novo em 1 min.";
}

function texto(tag, classe, conteudo) {
  const node = document.createElement(tag);
  if (classe) node.className = classe;
  if (conteudo !== undefined) node.textContent = conteudo;
  return node;
}

function marcaAtualizado() {
  const agora = new Date();
  el("atualizado-em").textContent =
    `atualizado ${String(agora.getHours()).padStart(2, "0")}h${String(agora.getMinutes()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------- /perto */

function classeDoTotal(total) {
  if (total <= 30) return "verde";
  if (total <= 60) return "amarelo";
  return "vermelho";
}

function cartaoAtracao(item, indice) {
  const cartao = texto("article", "cartao");
  cartao.appendChild(texto("div", "posicao", String(indice + 1)));

  const corpo = texto("div", "corpo");
  corpo.appendChild(texto("h2", null, item.name));
  const partes = [];
  partes.push(item.wait !== null ? `fila ${item.wait} min` : "fila —");
  if (item.walk !== null && item.meters !== null) {
    const fonte = item.route_source === "google" ? "Google" : "estimada";
    partes.push(`caminhada ${fonte} ${item.walk} min (${item.meters} m)`);
  }
  corpo.appendChild(texto("p", "detalhe", partes.join(" · ")));

  const selos = texto("div", "selos");
  if (item.quality !== null && item.quality >= 60) {
    selos.appendChild(texto("span", "selo bom", "Boa oportunidade agora"));
  }
  if (item.quality !== null) {
    selos.appendChild(texto("span", "selo", `qualidade da fila ⭐ ${item.quality}`));
  }
  if (item.coordinate) {
    // Era `item.coordinate && posicao`: sem GPS o link sumia, embora o /perto
    // só responda com GPS. Mantido pelo mesmo helper da aba Parques para as
    // duas telas não divergirem no formato do link.
    const selo = texto("span", "selo");
    selo.appendChild(linkDoMapa(item.coordinate, "🗺️ Google Maps"));
    selos.appendChild(selo);
  }
  if (selos.childElementCount) corpo.appendChild(selos);
  cartao.appendChild(corpo);

  if (item.total !== null) {
    const total = texto("div", `total ${classeDoTotal(item.total)}`);
    total.appendChild(texto("strong", null, String(item.total)));
    total.appendChild(texto("span", null, "total"));
    cartao.appendChild(total);
  }
  return cartao;
}

async function carregarPerto() {
  const destino = el("perto-conteudo");
  if (!posicao) return;
  try {
    const dados = await api(`/perto?lat=${posicao[0]}&lon=${posicao[1]}`);
    el("subtitulo").textContent = dados.park;
    el("atribuicao").textContent = dados.attribution;
    if (!dados.items.length) {
      // Tela em branco é o pior desfecho: parece falha de rede. Os dois
      // motivos de lista vazia são diferentes e o texto tem que dizer qual.
      destino.replaceChildren(texto("p", "aviso", dados.abertas
        ? "Nenhuma atração com fila utilizável agora — as leituras deste parque estão velhas ou sem coordenada."
        : "Parque fechado agora: nenhuma atração aberta. O ranking volta na abertura."));
    } else {
      destino.replaceChildren(...dados.items.map(cartaoAtracao));
    }
    marcaAtualizado();
  } catch (erro) {
    destino.replaceChildren(texto("div", "erro", mensagemDeErro(erro)));
  }
}

/* ------------------------------------------------------------ /vigias */

function cartaoVigia(vigia) {
  const cartao = texto("article", "cartao");
  const corpo = texto("div", "corpo");
  corpo.appendChild(texto("h2", null, vigia.ride));
  corpo.appendChild(texto("p", "detalhe", `${vigia.park} · vigia de ${vigia.quem}`));

  let alvoTxt;
  if (vigia.limite_pct !== null) {
    alvoTxt = vigia.alvo_min !== null
      ? `alvo ≤ ${vigia.alvo_min} min (${vigia.limite_pct}% do típico ~${vigia.tipico_min})`
      : `alvo ${vigia.limite_pct}% do típico — aguardando histórico`;
  } else {
    alvoTxt = `alvo ≤ ${vigia.limite_min} min`;
  }
  // Atração fechada publica wait_time 0, e "fila agora 0 min" convida a
  // caminhar até um brinquedo que não está funcionando. Pior: com limite
  // absoluto o 0 satisfaz qualquer alvo e a barra dizia "no alvo", prometendo
  // um alerta que o Telegram nunca manda — `maybe_alertar_fila_baixa` exige
  // is_open. A tela agora conta a mesma história que o alerta.
  const fechada = vigia.aberta === false;
  const filaTxt = fechada ? "fechada agora"
    : vigia.fila_agora !== null ? `fila agora ${vigia.fila_agora} min`
    : "fila agora —";
  corpo.appendChild(texto("p", "vigia-alvo", `${filaTxt} · ${alvoTxt}`));

  // Barra: quão perto a fila está do gatilho. 100% = dispararia agora.
  if (!fechada && vigia.fila_agora !== null && vigia.alvo_min !== null) {
    const progresso = Math.max(0, Math.min(100,
      Math.round(100 * vigia.alvo_min / Math.max(vigia.fila_agora, vigia.alvo_min))));
    const pronta = vigia.fila_agora <= vigia.alvo_min;
    const barra = texto("div", pronta ? "barra pronta" : "barra");
    const preenchido = texto("div");
    preenchido.style.width = `${pronta ? 100 : progresso}%`;
    barra.appendChild(preenchido);
    corpo.appendChild(barra);
    if (pronta) {
      corpo.appendChild(texto("p", "vigia-alvo", "✅ no alvo — o alerta do Telegram cuida do aviso"));
    }
  }
  cartao.appendChild(corpo);
  return cartao;
}

async function carregarVigias() {
  const destino = el("vigias-conteudo");
  try {
    const dados = await api("/vigias");
    el("atribuicao").textContent = dados.attribution;
    if (!dados.vigias.length) {
      destino.replaceChildren(texto("p", "aviso",
        "Nenhuma vigia ativa. Crie no Telegram: /vigiar everest 40"));
    } else {
      destino.replaceChildren(...dados.vigias.map(cartaoVigia));
    }
    marcaAtualizado();
  } catch (erro) {
    destino.replaceChildren(texto("div", "erro", mensagemDeErro(erro)));
  }
}

/* ----------------------------------------------------------- parques */

// As tags que o Telegram aceita e o projeto usa. Tudo fora daqui perde a tag e
// mantém só o texto.
const TAGS_TELEGRAM = new Set(["b", "strong", "i", "em", "u", "ins", "s",
                               "strike", "del", "code", "pre", "a", "br"]);
const ENTIDADES = { amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", apos: "'", nbsp: " " };

function desescapar(txt) {
  return txt.replace(/&(#?\w+);/g, (inteiro, nome) =>
    Object.prototype.hasOwnProperty.call(ENTIDADES, nome) ? ENTIDADES[nome] : inteiro);
}

/* Converte o HTML do Telegram em nós de verdade, SEM innerHTML.
 *
 * O texto vem do nosso próprio formatador e todo nome de atração já passou
 * pelo `notifier.esc` (regra 8), mas construir a árvore à mão em vez de
 * confiar nisso é o que torna impossível um `&` mal escapado virar execução.
 * Tag desconhecida perde a marcação e preserva o texto — some o negrito, nunca
 * a informação. */
function doTelegram(html) {
  const raiz = { filhos: [] };
  const pilha = [raiz];
  const padrao = /<(\/?)([a-zA-Z-]+)((?:\s[^>]*)?)>/g;
  let cursor = 0;
  let achado;
  const empurraTexto = (bruto) => {
    if (bruto) pilha[pilha.length - 1].filhos.push({ texto: desescapar(bruto) });
  };
  while ((achado = padrao.exec(html)) !== null) {
    empurraTexto(html.slice(cursor, achado.index));
    cursor = padrao.lastIndex;
    const [, fecha, nomeBruto, atributos] = achado;
    const nome = nomeBruto.toLowerCase();
    if (!TAGS_TELEGRAM.has(nome)) continue; // desconhecida: texto segue, tag some
    if (nome === "br") {
      pilha[pilha.length - 1].filhos.push({ texto: "\n" });
    } else if (fecha) {
      if (pilha.length > 1) pilha.pop();
    } else {
      const no = { tag: nome, filhos: [] };
      if (nome === "a") {
        const href = /href\s*=\s*"([^"]*)"/i.exec(atributos || "");
        // Só http(s): corta javascript: e data: na origem.
        if (href && /^https?:\/\//i.test(href[1])) no.href = desescapar(href[1]);
      }
      pilha[pilha.length - 1].filhos.push(no);
      pilha.push(no);
    }
  }
  empurraTexto(html.slice(cursor));

  const montar = (no, destino) => {
    for (const filho of no.filhos) {
      if (filho.texto !== undefined) {
        destino.appendChild(texto("span", null, filho.texto));
        continue;
      }
      const elemento = texto(filho.tag, null);
      if (filho.tag === "a" && filho.href) {
        elemento.href = filho.href;
        elemento.target = "_blank";
        elemento.rel = "noopener";
      }
      montar(filho, elemento);
      destino.appendChild(elemento);
    }
  };
  const bloco = texto("div", "telegram");
  montar(raiz, bloco);
  return bloco;
}

let parqueAtivo = null;
let comandosCache = null;

/* Link do Google Maps para uma coordenada.
 *
 * Com GPS, rota a pé; sem GPS, o ponto no mapa. Essa é a diferença que torna
 * o link útil fora do parque: `dir` exige origem e não abre nada sem ela,
 * `search` só precisa do destino. Quem está em casa planejando quer ver ONDE
 * fica; quem está lá dentro quer o caminho.
 */
function linkDoMapa(coordenada, rotulo) {
  const [lat, lon] = coordenada;
  const link = texto("a", "mapa", rotulo || "🗺️ mapa");
  link.href = posicao
    ? "https://www.google.com/maps/dir/?api=1"
      + `&origin=${posicao[0]},${posicao[1]}`
      + `&destination=${lat},${lon}&travelmode=walking`
    : `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function linhaAtracao(item, mostraFila) {
  const linha = texto("div", "linha");
  const nome = texto("span", null, item.ride);
  // Sem coordenada não há link: apontar o mapa para o centro do parque como
  // se fosse a atração seria coordenada inventada (regra 12).
  if (item.coordinate) nome.appendChild(linkDoMapa(item.coordinate));
  linha.appendChild(nome);
  if (!mostraFila) {
    // Show, trilha, exposição: a fila é 0 permanente, então some o número.
    // "0 min" diria que não há espera onde não há medição (regra 15).
    linha.appendChild(texto("span", item.aberta ? "fila-ok" : "",
                            item.aberta ? "em cartaz" : "fechada"));
    return linha;
  }
  const valor = !item.aberta ? "fechada" : item.wait === null ? "—" : `${item.wait} min`;
  linha.appendChild(texto("span", classeDaFila(item),
                          item.obsoleta ? `${valor} ⏳` : valor));
  return linha;
}

function secao(titulo, itens, mostraFila) {
  if (!itens.length) return null;
  const bloco = texto("section", "grupo");
  bloco.appendChild(texto("h2", "grupo-titulo", titulo));
  for (const item of itens) bloco.appendChild(linhaAtracao(item, mostraFila));
  return bloco;
}

async function rodarComando(cmd, botao, destino) {
  const anterior = botao.textContent;
  botao.disabled = true;
  botao.textContent = "…";
  try {
    const dados = await api(
      `/comando?cmd=${encodeURIComponent(cmd)}&parque=${encodeURIComponent(parqueAtivo)}`);
    destino.replaceChildren(doTelegram(dados.texto));
    marcaAtualizado();
  } catch (erro) {
    destino.replaceChildren(texto("div", "erro", mensagemDeErro(erro)));
  } finally {
    botao.disabled = false;
    botao.textContent = anterior;
  }
}

async function carregarParques() {
  const destino = el("parques-conteudo");
  try {
    if (!comandosCache) comandosCache = await api("/comandos");
    if (!parqueAtivo) parqueAtivo = comandosCache.parques[0];

    const tudo = texto("div", null);
    const seletor = texto("div", "escolha");
    for (const nome of comandosCache.parques) {
      const b = texto("button", nome === parqueAtivo ? "chip ativo" : "chip", nome);
      b.addEventListener("click", () => { parqueAtivo = nome; carregarParques(); });
      seletor.appendChild(b);
    }
    tudo.appendChild(seletor);

    const dados = await api(`/parque?nome=${encodeURIComponent(parqueAtivo)}`);
    el("subtitulo").textContent = dados.park;
    el("atribuicao").textContent = dados.attribution;

    const meta = [];
    if (dados.horario) {
      meta.push(`opera ~${String(dados.horario.abre).padStart(2, "0")}h–`
        + `${String(dados.horario.fecha).padStart(2, "0")}h pelo histórico`);
    }
    if (dados.lotacao && dados.lotacao.nivel) meta.push(`lotação ${dados.lotacao.nivel}`);
    if (dados.lotacao && dados.lotacao.fechadas) meta.push(`${dados.lotacao.fechadas} fechada(s)`);
    if (meta.length) tudo.appendChild(texto("p", "meta", meta.join(" · ")));

    const botoes = texto("div", "escolha comandos");
    const saida = texto("div", "saida-comando");
    for (const item of comandosCache.comandos) {
      const b = texto("button", "chip", item.rotulo);
      b.addEventListener("click", () => rodarComando(item.cmd, b, saida));
      botoes.appendChild(b);
    }
    tudo.appendChild(botoes);
    tudo.appendChild(saida);

    for (const bloco of [
      secao("Na watchlist", dados.items, true),
      secao("Outras atrações", dados.outras || [], true),
      secao("Shows e sem fila", dados.shows || [], false),
    ]) {
      if (bloco) tudo.appendChild(bloco);
    }
    destino.replaceChildren(tudo);
    marcaAtualizado();
  } catch (erro) {
    destino.replaceChildren(texto("div", "erro", mensagemDeErro(erro)));
  }
}

/* ------------------------------------------------------------ roteiro */

const MES_CURTO = ["jan", "fev", "mar", "abr", "mai", "jun",
                   "jul", "ago", "set", "out", "nov", "dez"];
const DIA_SEMANA = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];
let roteiroCache = null;

function hojeNoParque() {
  // "Hoje" no fuso dos parques, não no do celular: às 23h de Brasília ainda
  // são 22h em Orlando, e virar o cartão do dia uma hora antes confundiria.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
}

/* Cor da fila. Recebe o item inteiro porque `aberta` faz parte da decisão:
 * atração fechada publica wait 0, que passa em qualquer threshold, e pintar
 * "fechada" de verde dizia "pode ir agora" sobre um brinquedo parado. */
function classeDaFila(item) {
  if (item.aberta === false) return "";
  if (item.wait === null || item.wait === undefined) return "";
  if (item.threshold === null || item.threshold === undefined) return "";
  return item.wait <= item.threshold ? "fila-ok" : "fila-alta";
}

async function abrirFilasDoDia(dia, container, botao) {
  botao.disabled = true;
  botao.textContent = "carregando…";
  try {
    const dados = await api(`/parque?nome=${encodeURIComponent(dia.parque)}`);
    const bloco = texto("div", "filas-live");
    const meta = [];
    if (dados.horario) meta.push(`opera ~${String(dados.horario.abre).padStart(2, "0")}h–${String(dados.horario.fecha).padStart(2, "0")}h pelo histórico`);
    if (dados.lotacao && dados.lotacao.nivel) meta.push(`lotação ${dados.lotacao.nivel}`);
    if (dados.lotacao && dados.lotacao.fechadas) meta.push(`${dados.lotacao.fechadas} fechada(s)`);
    if (meta.length) bloco.appendChild(texto("p", "meta", meta.join(" · ")));
    for (const item of dados.items) {
      const linha = texto("div", "linha");
      const nome = texto("span", null, item.ride);
      const partes = [];
      if (item.duracao_min !== null) partes.push(`~${item.duracao_min} min de atração`);
      if (partes.length) nome.appendChild(texto("small", "detalhe", ` ${partes.join(" · ")}`));
      if (item.coordinate) nome.appendChild(linkDoMapa(item.coordinate));
      linha.appendChild(nome);
      const valor = !item.aberta ? "fechada"
        : item.wait === null ? "—"
        : `${item.wait} min`;
      linha.appendChild(texto("span", classeDaFila(item),
                              item.obsoleta ? `${valor} ⏳` : valor));
      bloco.appendChild(linha);
    }
    botao.replaceWith(bloco);
    marcaAtualizado();
  } catch (erro) {
    botao.disabled = false;
    botao.textContent = "ver filas agora";
    container.appendChild(texto("div", "erro", mensagemDeErro(erro)));
  }
}

function cartaoDia(dia, hoje) {
  const [ano, mes, diaNum] = dia.data.split("-").map(Number);
  const cartao = texto("article", dia.data === hoje ? "dia hoje" : "dia");

  const cab = texto("div", "dia-cabecalho");
  const data = texto("div", "dia-data");
  data.appendChild(texto("strong", null, String(diaNum)));
  data.appendChild(texto("span", null,
    `${MES_CURTO[mes - 1]} · ${DIA_SEMANA[new Date(ano, mes - 1, diaNum).getDay()]}`));
  cab.appendChild(data);
  const titulo = texto("div", "dia-titulo");
  titulo.appendChild(texto("h2", null, dia.rotulo));
  titulo.appendChild(texto("p", null, dia.destaque));
  cab.appendChild(titulo);
  if (dia.data === hoje) cab.appendChild(texto("span", "selo-hoje", "HOJE"));
  cartao.appendChild(cab);

  if (dia.timeline.length) {
    const detalhes = texto("details");
    if (dia.data === hoje) detalhes.open = true;
    detalhes.appendChild(texto("summary", null, "plano do dia"));
    const lista = texto("ul", "dia-timeline");
    for (const passo of dia.timeline) {
      const li = texto("li");
      li.appendChild(texto("b", null, passo.hora));
      li.appendChild(texto("span", null, passo.texto));
      lista.appendChild(li);
    }
    if (dia.furafila) {
      const li = texto("li");
      li.appendChild(texto("b", null, "Fura-fila"));
      li.appendChild(texto("span", null, dia.furafila));
      lista.appendChild(li);
    }
    detalhes.appendChild(lista);
    cartao.appendChild(detalhes);
  }
  if (dia.notas) cartao.appendChild(texto("p", "dia-nota", dia.notas));
  if (dia.analise_troca) {
    cartao.appendChild(texto("p", "dia-troca", `🔀 ${dia.analise_troca}`));
  }
  if (dia.parque) {
    const botao = texto("button", "botao-filas", "ver filas agora");
    botao.addEventListener("click", () => abrirFilasDoDia(dia, cartao, botao));
    cartao.appendChild(botao);
  }
  return cartao;
}

async function carregarRoteiro() {
  const destino = el("roteiro-conteudo");
  try {
    if (!roteiroCache) {
      const resp = await fetch("roteiro.json");
      if (!resp.ok) throw new Error(`roteiro.json: HTTP ${resp.status}`);
      roteiroCache = await resp.json();
    }
    const hoje = hojeNoParque();
    destino.replaceChildren(
      texto("p", "aviso", roteiroCache.subtitulo),
      ...roteiroCache.dias.map((dia) => cartaoDia(dia, hoje)));
    const cartaoHoje = destino.querySelector(".dia.hoje");
    if (cartaoHoje) cartaoHoje.scrollIntoView({ block: "start", behavior: "instant" });
  } catch (erro) {
    destino.replaceChildren(texto("div", "erro", String(erro.message || erro)));
  }
}

/* ----------------------------------------------------- ciclo e abas */

function recarregar() {
  if (abaAtiva === "perto") carregarPerto();
  else if (abaAtiva === "parques") carregarParques();
  else if (abaAtiva === "roteiro") carregarRoteiro();
  else carregarVigias();
}

function agendar() {
  clearInterval(timer);
  timer = setInterval(recarregar, ATUALIZA_MS);
}

function trocarAba(nome) {
  abaAtiva = nome;
  document.querySelectorAll(".aba").forEach((aba) =>
    aba.classList.toggle("ativa", aba.dataset.aba === nome));
  for (const painel of ["perto", "parques", "roteiro", "vigias"]) {
    el(`painel-${painel}`).classList.toggle("oculto", nome !== painel);
  }
  try { localStorage.setItem("aba", nome); } catch { /* modo privado: segue sem lembrar */ }
  recarregar();
  agendar();
}

function iniciarGPS() {
  if (!navigator.geolocation) {
    el("perto-conteudo").replaceChildren(
      texto("div", "erro", "Este navegador não fornece localização."));
    return;
  }
  navigator.geolocation.watchPosition(
    (leitura) => {
      const primeira = posicao === null;
      posicao = [leitura.coords.latitude, leitura.coords.longitude];
      if (primeira && abaAtiva === "perto") carregarPerto();
    },
    () => {
      if (!posicao) {
        el("perto-conteudo").replaceChildren(texto("div", "erro",
          "Sem acesso à localização. Libere nas permissões do navegador e recarregue."));
      }
    },
    { enableHighAccuracy: true, maximumAge: 30_000, timeout: 15_000 },
  );
}

document.querySelectorAll(".aba").forEach((aba) =>
  aba.addEventListener("click", () => trocarAba(aba.dataset.aba)));
el("atualizar").addEventListener("click", () => {
  el("atualizar").classList.add("girando");
  setTimeout(() => el("atualizar").classList.remove("girando"), 1000);
  recarregar();
});

let abaInicial = "perto";
try { abaInicial = localStorage.getItem("aba") || "perto"; } catch { /* idem */ }
trocarAba(abaInicial);
iniciarGPS();
