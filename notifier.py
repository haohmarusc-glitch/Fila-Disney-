"""Notificador Telegram — envia alertas de fila e recebe comandos do chat."""
import html
import logging
import os

import requests

log = logging.getLogger("notifier")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

HTTP_TIMEOUT = 10
POLL_TIMEOUT = 20  # long polling: quanto o Telegram segura a conexão sem novidade


def configured() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


def esc(text) -> str:
    """Escapa para parse_mode HTML.

    Obrigatório em qualquer nome vindo da API: "Mickey & Minnie's Runaway Railway"
    com o & cru faz o Telegram devolver 400 e a mensagem não sai.
    """
    return html.escape(str(text), quote=False)


BOTAO_LOCALIZACAO = {
    "keyboard": [[{"text": "📍 Enviar minha localização", "request_location": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}


def send(text: str, reply_markup: dict | None = None, chat_id=None) -> bool:
    """Envia mensagem ao Telegram. Retorna True em caso de sucesso."""
    if not configured():
        log.warning("Telegram não configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Msg: %s", text)
        return False
    corpo = {
        "chat_id": CHAT_ID if chat_id is None else chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,  # link de rota não vira card gigante
    }
    if reply_markup:
        corpo["reply_markup"] = reply_markup
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json=corpo, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            log.error("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Falha ao enviar Telegram: %s", exc)
        return False


def get_updates(offset: int | None = None, timeout: int = POLL_TIMEOUT) -> list[dict]:
    """Long polling do getUpdates. Devolve [] em qualquer falha.

    Quem chama está dentro do loop principal: nenhuma falha de rede daqui pode
    virar exceção lá em cima.
    """
    if not configured():
        return []
    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            log.error("getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("result", [])
    except (requests.RequestException, ValueError) as exc:
        log.error("Falha no getUpdates: %s", exc)
        return []


def drop_pending_updates() -> int | None:
    """Descarta o backlog acumulado enquanto o bot esteve fora do ar.

    Sem isso, ao subir o container o bot responderia de uma vez a todo /status
    mandado durante a queda. Devolve o offset inicial (último update_id + 1).
    """
    updates = get_updates(timeout=0)
    if not updates:
        return None
    log.info("Descartados %d update(s) antigos do Telegram", len(updates))
    return updates[-1]["update_id"] + 1


def is_authorized(chat_id) -> bool:
    """Só o chat configurado manda comandos — o token do bot vaza fácil."""
    return str(chat_id) == str(CHAT_ID)


def format_alert(park: str, ride: str, wait: int, threshold: int, tendencia: str = "") -> str:
    return (
        f"🎢 <b>{esc(ride)}</b>\n"
        f"⏱ Fila agora: <b>{wait} min</b>{tendencia} (alerta ≤ {threshold} min)\n"
        f"📍 {esc(park)}\n"
        f"➡️ Vai agora!"
    )
