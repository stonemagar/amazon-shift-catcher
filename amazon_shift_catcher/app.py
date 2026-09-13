import json
import logging
import os
import re
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

EMPTY_SCHEDULE_RE = re.compile(
    r"Sorry,\s*we do not have any schedules matching your preferences\.\s*"
    r"We post schedules from Monday to Friday each week\.\s*"
    r"Please try again at a later time\.",
    re.I,
)


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


def choose_amazon_page(browser, my_jobs_url: str):
    """Prefer an already-open Amazon Jobs tab in the attached Brave session."""
    pages = []
    for context in browser.contexts:
        pages.extend(context.pages)

    for page in pages:
        try:
            if "jobsatamazon.co.uk" in page.url.lower():
                return page
        except Exception:
            pass

    if not browser.contexts:
        raise RuntimeError("No browser context found in the attached Brave session.")

    context = browser.contexts[0]
    page = context.new_page()
    page.goto(my_jobs_url, wait_until="domcontentloaded")
    return page


def get_page_status(page) -> tuple[str, str, str | None, str]:
    """Return URL, title, recognized empty state, and a short visible-text excerpt."""
    try:
        url = page.url
    except Exception:
        url = "<unavailable>"

    try:
        title = " ".join(page.title().split()) or "<no title>"
    except Exception:
        title = "<unavailable>"

    empty_state = None
    excerpt = "<unavailable>"
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        normalized = " ".join(body_text.split())
        excerpt = normalized[:500] if normalized else "<empty body>"

        match = EMPTY_SCHEDULE_RE.search(body_text)
        if match:
            empty_state = " ".join(match.group(0).split())
    except Exception:
        pass

    return url, title, empty_state, excerpt


def run():
    load_dotenv(ROOT / ".env")
    setup_logging()
    log = logging.getLogger("shift-catcher")

    config = load_config()
    rules = config["rules"]
    interval = max(3, int(config.get("check_interval_seconds", 10)))
    auto_apply = env_bool("AUTO_APPLY", False)
    cdp_url = os.getenv("BRAVE_CDP_URL", "http://127.0.0.1:9222").strip()

    log.info("Starting Amazon Shift Catcher")
    log.info("AUTO_APPLY=%s", auto_apply)
    log.info("Check interval=%ss", interval)
    log.info("Connecting to existing Brave session at %s", cdp_url)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                "Could not connect to Brave. Start Brave with remote debugging on port 9222, "
                "log in to Amazon Jobs, leave Brave open, then run this program."
            ) from exc

        page = choose_amazon_page(browser, config["my_jobs_url"])
        page.set_default_timeout(int(config.get("navigation_timeout_seconds", 30)) * 1000)

        try:
            reach_shift_page(
                page,
                config["my_jobs_url"],
                config.get("job_id") or None,
            )

            # Amazon's schedule page is a JavaScript app. Give it a moment to
            # render its schedule cards or no-schedules message after navigation.
            page.wait_for_timeout(1500)

            send_notification(
                "Amazon Shift Catcher connected to your existing Brave session.\n"
                f"Monitoring every {interval} seconds.\n"
                f"Auto Apply: {'ON' if auto_apply else 'OFF'}",
                title="Amazon monitor started",
                priority=3,
            )

            alerted_for_current_batch = False

            while True:
                if page.is_closed():
                    raise RuntimeError("The Amazon Jobs tab was closed.")

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

                    url, title, empty_state, excerpt = get_page_status(page)
                    if empty_state:
                        log.info(
                            'No shift card | Page="%s" | URL=%s | Empty state="%s"',
                            title,
                            url,
                            empty_state,
                        )
                    else:
                        log.warning(
                            'No shift card | Page="%s" | URL=%s | '
                            'Expected Amazon empty-state text was NOT found | Visible text="%s"',
                            title,
                            url,
                            excerpt,
                        )

                time.sleep(interval)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                except Exception:
                    log.exception("Reload failed. Retrying after interval.")

        except KeyboardInterrupt:
            log.info("Stopped by user.")
        except Exception as exc:
            log.exception("Fatal error")
            start_alarm()
            try:
                if not page.is_closed():
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
            log.info("Disconnected from automation. Brave has been left open.")
