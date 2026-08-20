"""Notificador Telegram — envia alertas de fila para o chat configurado."""
import logging
import os

import requests

log = logging.getLogger("notifier")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def send(text: str) -> bool:
    """Envia mensagem ao Telegram. Retorna True em caso de sucesso."""
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram não configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Msg: %s", text)
        return False
    try:
        resp = requests.post(
            API_URL,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Falha ao enviar Telegram: %s", exc)
        return False


def format_alert(park: str, ride: str, wait: int, threshold: int) -> str:
    return (
        f"🎢 <b>{ride}</b>\n"
        f"⏱ Fila agora: <b>{wait} min</b> (alerta ≤ {threshold} min)\n"
        f"📍 {park}\n"
        f"➡️ Vai agora!"
    )
