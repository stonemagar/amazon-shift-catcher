import atexit

from amazon_shift_catcher.app import run
from amazon_shift_catcher.notifier import send_notification


def notify_shutdown() -> None:
    """Notify when Python exits normally or after a handled Ctrl+C/error."""
    try:
        send_notification(
            "Amazon Shift Catcher has stopped running.\n"
            "The Brave browser may still be open, but automatic monitoring is OFF.",
            title="Amazon monitor SHUTDOWN",
            priority=5,
        )
    except Exception:
        # Shutdown must never hang or crash because notification delivery failed.
        pass


if __name__ == "__main__":
    atexit.register(notify_shutdown)
    run()
