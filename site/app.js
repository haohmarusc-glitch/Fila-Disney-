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
  if (item.coordinate && posicao) {
    const selo = texto("span", "selo");
    const link = texto("a", null, "🗺️ Google Maps");
    link.href = "https://www.google.com/maps/dir/?api=1"
      + `&origin=${posicao[0]},${posicao[1]}`
      + `&destination=${item.coordinate[0]},${item.coordinate[1]}`
      + "&travelmode=walking";
    link.target = "_blank";
    link.rel = "noopener";
    selo.appendChild(link);
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
    destino.replaceChildren(...dados.items.map(cartaoAtracao));
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
  const filaTxt = vigia.fila_agora !== null ? `fila agora ${vigia.fila_agora} min` : "fila agora —";
  corpo.appendChild(texto("p", "vigia-alvo", `${filaTxt} · ${alvoTxt}`));

  // Barra: quão perto a fila está do gatilho. 100% = dispararia agora.
  if (vigia.fila_agora !== null && vigia.alvo_min !== null) {
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

/* ----------------------------------------------------- ciclo e abas */

function recarregar() {
  if (abaAtiva === "perto") carregarPerto();
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
  el("painel-perto").classList.toggle("oculto", nome !== "perto");
  el("painel-vigias").classList.toggle("oculto", nome !== "vigias");
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
