import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from .alarm import start_alarm, stop_alarm
from .browser import (
    page_has_security_challenge,
    reach_shift_page,
    read_shift_cards,
    save_screenshot,
)
from .notifier import send_notification
from .rules import parse_shift, matches_rules, score_shift


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
PROFILE_DIR = ROOT / "browser-profile"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / "shift-catcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def load_config():
    config_path = ROOT / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            "config.json not found. Copy config.example.json to config.json first."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def timestamp_name(prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return SCREENSHOT_DIR / f"{prefix}-{stamp}.png"


def find_browser_executable() -> str | None:
    """Prefer a user-configured browser path, then auto-detect Brave on Windows."""
    configured = os.getenv("BROWSER_EXECUTABLE_PATH", "").strip()
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        path = Path(expanded)
        if not path.exists():
            raise FileNotFoundError(
                f"BROWSER_EXECUTABLE_PATH does not exist: {expanded}"
            )
        return str(path)

    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
            Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            candidates.append(
                Path(local_app_data)
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe"
            )

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    return None


def run():
    load_dotenv(ROOT / ".env")
    setup_logging()
    log = logging.getLogger("shift-catcher")

    config = load_config()
    rules = config["rules"]
    interval = max(3, int(config.get("check_interval_seconds", 10)))
    auto_apply = env_bool("AUTO_APPLY", False)
    browser_executable = find_browser_executable()

    log.info("Starting Amazon Shift Catcher")
    log.info("AUTO_APPLY=%s", auto_apply)
    log.info("Check interval=%ss", interval)
    if browser_executable:
        log.info("Browser executable=%s", browser_executable)
    else:
        log.info("No external browser configured/found; using Playwright Chromium")

    with sync_playwright() as p:
        launch_kwargs = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": False,
            "viewport": {"width": 1365, "height": 768},
            "chromium_sandbox": True,
        }
        if browser_executable:
            launch_kwargs["executable_path"] = browser_executable

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(int(config.get("navigation_timeout_seconds", 30)) * 1000)

        try:
            reach_shift_page(
                page,
                config["my_jobs_url"],
                config.get("job_id") or None,
            )

            send_notification(
                "Amazon Shift Catcher started.\n"
                f"Monitoring every {interval} seconds.\n"
                f"Auto Apply: {'ON' if auto_apply else 'OFF'}",
                title="Amazon monitor started",
                priority=3,
            )

            alerted_for_current_batch = False

            while True:
                if page_has_security_challenge(page):
                    start_alarm()
                    save_screenshot(page, timestamp_name("security-challenge"))
                    send_notification(
                        "Amazon security or verification page detected. "
                        "Automation has stopped. Complete it manually.",
                        title="Amazon action required",
                        priority=5,
                    )
                    log.error("Security challenge detected. Stopping.")
                    break

                cards = read_shift_cards(page)

                if cards:
                    log.info("%d visible Apply button(s) / shift card(s) detected", len(cards))

                    if not alerted_for_current_batch:
                        start_alarm()
                        save_screenshot(page, timestamp_name("shift-found"))
                        send_notification(
                            f"{len(cards)} available shift option(s) detected.\n"
                            "Checking your rules now.",
                            title="AMAZON SHIFT FOUND",
                            priority=5,
                        )
                        alerted_for_current_batch = True

                    matches = []
                    for card in cards:
                        shift = parse_shift(card["text"])
                        ok, reasons = matches_rules(shift, rules)
                        log.info(
                            "Card %s | match=%s | hours=%s | days=%s | time=%s-%s | %s",
                            card["index"],
                            ok,
                            shift.hours_per_week,
                            shift.days,
                            shift.start_time,
                            shift.end_time,
                            "; ".join(reasons) if reasons else "accepted",
                        )
                        if ok:
                            matches.append((score_shift(shift, rules), card, shift))

                    if matches:
                        matches.sort(key=lambda item: item[0], reverse=True)
                        _, best_card, best_shift = matches[0]

                        details = (
                            f"Hours: {best_shift.hours_per_week or 'unknown'}\n"
                            f"Days: {', '.join(best_shift.days) or 'unknown'}\n"
                            f"Time: {best_shift.start_time or '?'} - "
                            f"{best_shift.end_time or '?'}"
                        )

                        if auto_apply:
                            log.warning("Matching shift found. Clicking Apply.")
                            save_screenshot(page, timestamp_name("before-apply"))
                            best_card["button"].click()
                            page.wait_for_timeout(1500)
                            save_screenshot(page, timestamp_name("after-apply"))
                            stop_alarm()

                            send_notification(
                                details
                                + "\n\nApply was clicked. Version 1 stops here. "
                                "Check the next Amazon page.",
                                title="AMAZON SHIFT APPLY CLICKED",
                                priority=5,
                            )
                            log.info("Apply clicked. Version 1 complete.")
                            break
                        else:
                            send_notification(
                                details
                                + "\n\nAUTO_APPLY is OFF, so Apply was not clicked.",
                                title="MATCHING AMAZON SHIFT FOUND",
                                priority=5,
                            )
                            log.warning("Matching shift found but AUTO_APPLY is OFF.")
                    else:
                        log.info("Shift cards exist, but none match the configured rules.")

                else:
                    if alerted_for_current_batch:
                        stop_alarm()
                    alerted_for_current_batch = False
                    log.info("No available shift card detected.")

                time.sleep(interval)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    log.exception("Reload failed. Retrying after interval.")

        except KeyboardInterrupt:
            log.info("Stopped by user.")
        except Exception as exc:
            log.exception("Fatal error")
            start_alarm()
            try:
                save_screenshot(page, timestamp_name("fatal-error"))
            except Exception:
                pass
            send_notification(
                f"Amazon Shift Catcher stopped with an error:\n{exc}",
                title="Amazon monitor stopped",
                priority=5,
            )
        finally:
            stop_alarm()
            context.close()
