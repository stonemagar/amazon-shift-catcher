import logging
import os
import requests

log = logging.getLogger(__name__)


def telegram_enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.info("Telegram not configured. Message: %s", message)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception:
        log.exception("Telegram notification failed")
        return False
