import atexit

from amazon_shift_catcher.job_search_app import run
from amazon_shift_catcher.notifier import send_notification


def notify_shutdown() -> None:
    """Notify when the separate job-search monitor stops."""
    try:
        send_notification(
            "Amazon Job Search catcher has stopped running.\n"
            "The Brave browser may still be open, but job-search monitoring is OFF.",
            title="Amazon Job Search monitor SHUTDOWN",
            priority=5,
        )
    except Exception:
        pass


if __name__ == "__main__":
    atexit.register(notify_shutdown)
    run()
