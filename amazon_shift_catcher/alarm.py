import threading
import time
import sys

_stop_event = threading.Event()
_thread = None


def _alarm_loop():
    if sys.platform.startswith("win"):
        import winsound
        while not _stop_event.is_set():
            winsound.Beep(2600, 700)
            if _stop_event.is_set():
                break
            winsound.Beep(3300, 700)
            time.sleep(0.15)
    else:
        while not _stop_event.is_set():
            print("\a", end="", flush=True)
            time.sleep(1)


def start_alarm():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_alarm_loop, daemon=True)
    _thread.start()


def stop_alarm():
    _stop_event.set()


def play_alarm_once():
    """Play one short alert sound without starting the repeating alarm loop."""
    def _play_once():
        if sys.platform.startswith("win"):
            import winsound
            winsound.Beep(2600, 700)
        else:
            print("\\a", end="", flush=True)

    threading.Thread(target=_play_once, daemon=True).start()
