import logging
import os
import requests

log = logging.getLogger(__name__)


def _send_ntfy(message: str, title: str = "Amazon Shift Catcher", priority: int = 5) -> bool:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")

    if not topic:
        return False

    try:
        response = requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": str(priority),
                "Tags": "rotating_light,package",
            },
            timeout=10,
        )
        response.raise_for_status()
        log.info("ntfy notification sent")
        return True
    except Exception:
        log.exception("ntfy notification failed")
        return False


def _send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        log.info("Telegram notification sent")
        return True
    except Exception:
        log.exception("Telegram notification failed")
        return False


def send_notification(message: str, title: str = "Amazon Shift Catcher", priority: int = 5) -> bool:
    """
    Send a phone notification using the first configured provider.

    Provider order:
    1. ntfy
    2. Telegram

    The local loud alarm is handled separately, so a notification-provider
    failure does not stop the shift catcher.
    """
    if _send_ntfy(message, title=title, priority=priority):
        return True

    if _send_telegram(message):
        return True

    log.info("No phone notification provider configured. Message: %s", message)
    return False
