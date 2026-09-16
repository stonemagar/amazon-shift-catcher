import logging
import os

import requests

log = logging.getLogger(__name__)


def _send_ntfy(
    message: str,
    title: str = "Amazon Shift Catcher",
    priority: int = 5,
) -> bool:
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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        retry_after = (
            exc.response.headers.get("Retry-After")
            if exc.response is not None
            else None
        )
        if status == 429:
            log.warning(
                "ntfy notification was rate-limited (HTTP 429). Retry-After=%s. "
                "No immediate retry will be made; another configured provider will be tried.",
                retry_after or "not provided",
            )
        else:
            log.exception("ntfy notification failed with HTTP status %s", status)
        return False
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


def send_notification(
    message: str,
    title: str = "Amazon Shift Catcher",
    priority: int = 5,
) -> bool:
    """
    Send a phone notification using the first provider that succeeds.

    Provider order:
    1. ntfy
    2. Telegram fallback

    A provider failure never stops the shift catcher. A 429 from ntfy is not
    retried immediately because that would add more requests while the provider
    is rate-limiting us; Telegram is tried next when configured.
    """
    ntfy_configured = bool(os.getenv("NTFY_TOPIC", "").strip())
    telegram_configured = bool(
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        and os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )

    if _send_ntfy(message, title=title, priority=priority):
        return True

    if _send_telegram(message):
        return True

    if ntfy_configured or telegram_configured:
        log.error(
            "Phone notification was NOT delivered. All configured providers failed."
        )
    else:
        log.warning(
            "No phone notification provider is configured. Message was not delivered."
        )
    return False
