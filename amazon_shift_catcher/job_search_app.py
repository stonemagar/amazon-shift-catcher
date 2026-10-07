import json
import logging
import os
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from .alarm import start_alarm, stop_alarm
from .job_search_browser import (
    card_requires_detail_location_check,
    detect_application_stage,
    find_apply_button,
    normalise_location_terms,
    open_job_candidate,
    open_job_search,
    page_is_blocked,
    read_job_detail_text,
    read_warehouse_job_cards,
    return_to_job_search,
    text_matches_location,
    wait_for_manual_login,
)
from .location_storage import save_unique_locations
from .notifier import send_notification


ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
LOCATION_STORE = ROOT / "data" / "job_search_locations.json"


class JobSearchState(str, Enum):
    STARTING = "STARTING"
    JOB_SEARCH = "JOB_SEARCH"
    CHECKING_JOB = "CHECKING_JOB"
    JOB_FOUND = "JOB_FOUND"
    APPLICATION_GUIDE = "APPLICATION_GUIDE"
    SHIFT_SELECTION = "SHIFT_SELECTION"
    SECURITY_CHECK = "SECURITY_CHECK"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / "job-search-catcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def load_config() -> dict:
    config_path = ROOT / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            "config.json not found. Copy config.example.json to config.json first."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def choose_desired_locations() -> list[str]:
    print("\n" + "=" * 64)
    print("AMAZON JOB SEARCH METHOD - LOCATION")
    print("Enter the location/postcode/warehouse area you want.")
    print("You can enter alternatives separated by commas.")
    print("Example: Wembley, Croydon, DA1")
    print("Press Enter with no value to accept the first Warehouse Associate job.")
    print("=" * 64)

    raw = input("Desired location(s): ").strip()
    selected = normalise_location_terms(raw)

    if selected:
        print("Monitoring location(s): " + ", ".join(selected))
    else:
        print("Monitoring location: ANY")

    return selected


def _choose_amazon_page(browser, fallback_url: str):
    pages = []
    for context in browser.contexts:
        pages.extend(context.pages)

    for page in pages:
        try:
            if "jobsatamazon.co.uk" in page.url.lower():
                return page
        except Exception:
            continue

    if not browser.contexts:
        raise RuntimeError("No browser context found in the attached Brave session.")

    page = browser.contexts[0].new_page()
    page.goto(fallback_url, wait_until="domcontentloaded")
    return page


def _screenshot_path(prefix: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return SCREENSHOT_DIR / f"{prefix}-{stamp}.png"


def _save_screenshot(page, prefix: str, log) -> None:
    try:
        page.screenshot(path=str(_screenshot_path(prefix)), full_page=True)
    except Exception as exc:
        log.warning("Could not save screenshot: %s", exc)


def _notify(body: str, **kwargs) -> None:
    try:
        send_notification(body, **kwargs)
    except Exception:
        pass


def find_desired_job(page, desired_locations: list[str], log):
    """Use the location shown on each result card before opening any job.

    A normal card already exposes its place/location, so non-matching cards are skipped
    without navigation. Only cards that explicitly say "multiple on site locations
    available" are opened to inspect the detailed location.
    """
    cards = read_warehouse_job_cards(page)
    if not cards:
        return None

    log.info("Found %s visible Warehouse Associate card(s).", len(cards))

    visible_locations = [
        candidate.location
        for candidate in cards
        if candidate.location
    ]
    added_locations = save_unique_locations(LOCATION_STORE, visible_locations)
    if added_locations:
        log.info(
            "Saved %s new unique card location(s): %s",
            len(added_locations),
            ", ".join(added_locations),
        )

    for index in range(len(cards)):
        current_cards = read_warehouse_job_cards(page)
        if index >= len(current_cards):
            break

        candidate = current_cards[index]
        log.info(
            "Checking Warehouse Associate card %s/%s: %s",
            index + 1,
            len(cards),
            candidate.text[:350] or "<no readable card text>",
        )

        visible_location_matches = text_matches_location(
            candidate.location or candidate.text,
            desired_locations,
        )

        if visible_location_matches:
            log.info(
                "Desired location matched directly on the result card. "
                "Opening this matching card only."
            )
            open_job_candidate(page, candidate)

            if page_is_blocked(page):
                raise RuntimeError("Amazon returned a 403 / request-blocked page.")

            detail_text = read_job_detail_text(page)
            apply_button = find_apply_button(page)
            if apply_button is None:
                log.warning(
                    "Location matched, but no visible Apply button was found on the details page."
                )
            return {
                "card_text": candidate.text,
                "detail_text": detail_text,
                "apply_visible": apply_button is not None,
                "url": page.url,
            }

        if not card_requires_detail_location_check(candidate.text):
            log.info(
                "Visible card location does not match. Skipping this card without opening it."
            )
            continue

        log.info(
            "Card says multiple on site locations available. "
            "Opening it to check the detailed location."
        )
        open_job_candidate(page, candidate)

        if page_is_blocked(page):
            raise RuntimeError("Amazon returned a 403 / request-blocked page.")

        detail_text = read_job_detail_text(page)
        if text_matches_location(detail_text, desired_locations):
            apply_button = find_apply_button(page)
            if apply_button is None:
                log.warning(
                    "Detailed location matched, but no visible Apply button was found."
                )
            return {
                "card_text": candidate.text,
                "detail_text": detail_text,
                "apply_visible": apply_button is not None,
                "url": page.url,
            }

        log.info(
            "Detailed location did not match this multiple-location card. "
            "Returning to search results."
        )
        return_to_job_search(page)

    return None


def guide_user_to_shift_selection(page, log) -> bool:
    """Watch the application flow while the user performs the clicks manually."""
    last_stage = None

    print("\nMANUAL APPLICATION FLOW")
    print("Complete the next Amazon button shown in Brave.")
    print("The catcher will detect each page change and guide you.")
    print("It will stop automatically when the shift-selection page is reached.\n")

    while not page.is_closed():
        if page_is_blocked(page):
            raise RuntimeError("Amazon returned a 403 / request-blocked page.")

        stage = detect_application_stage(page)
        if stage != last_stage:
            last_stage = stage
            log.info("Application stage detected: %s", stage)

            if stage == "APPLY":
                print("Next action in Brave: click Apply")
            elif stage == "NEXT":
                print("Next action in Brave: click Next")
            elif stage == "START_APPLICATION":
                print("Next action in Brave: click Start Application")
            elif stage == "CONTINUE":
                print("Next action in Brave: click Continue")
            elif stage == "SECURITY_CHECK":
                print("Amazon security/verification step detected. Complete it manually.")
            elif stage == "SHIFT_SELECTION":
                print("Shift-selection page reached.")
                return True
            elif stage == "UNKNOWN":
                print(
                    "Waiting for the next known Amazon application page. "
                    "If Amazon is still loading, wait a moment."
                )

        page.wait_for_timeout(500)

    return False


def run() -> None:
    load_dotenv(ROOT / ".env")
    config = load_config()
    desired_locations = choose_desired_locations()

    setup_logging()
    log = logging.getLogger("job-search-catcher")

    # Create the local location store immediately so it is easy to find even
    # before the first new location is discovered.
    save_unique_locations(LOCATION_STORE, [])
    log.info("Location storage file=%s", LOCATION_STORE)

    interval = max(3, int(config.get("check_interval_seconds", 10)))
    timeout_ms = int(config.get("navigation_timeout_seconds", 30)) * 1000
    cdp_url = os.getenv("BRAVE_CDP_URL", "http://127.0.0.1:9222").strip()

    state = JobSearchState.STARTING
    stop_reason = "normal shutdown"

    log.info("Starting separate Amazon Job Search catching method.")
    log.info("Existing Method 1 code path is not being used or modified.")
    log.info("Check interval=%ss", interval)
    log.info(
        "Desired locations=%s",
        ", ".join(desired_locations) if desired_locations else "ANY",
    )
    log.info("Connecting to existing Brave session at %s", cdp_url)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                "Could not connect to Brave. Start Brave with remote debugging on port 9222 "
                "and leave the browser open."
            ) from exc

        page = _choose_amazon_page(
            browser,
            "https://www.jobsatamazon.co.uk/app#/jobSearch",
        )
        page.set_default_timeout(timeout_ms)

        try:
            wait_for_manual_login(page)
            state = JobSearchState.JOB_SEARCH

            _notify(
                "Amazon Job Search catcher is active.\n"
                "Search: Warehouse Associate\n"
                f"Location: {', '.join(desired_locations) if desired_locations else 'ANY'}\n"
                f"Check interval: {interval} seconds.",
                title="Amazon Job Search monitor started",
                priority=3,
            )

            while True:
                if page.is_closed():
                    raise RuntimeError("The Amazon Jobs tab was closed.")

                if page_is_blocked(page):
                    state = JobSearchState.BLOCKED
                    _save_screenshot(page, "job-search-403", log)
                    start_alarm()
                    _notify(
                        "Amazon Job Search returned a 403 / request-blocked page. "
                        "Automation has stopped. Check Brave manually.",
                        title="Amazon Job Search blocked",
                        priority=5,
                    )
                    stop_reason = "403 blocked"
                    break

                state = JobSearchState.CHECKING_JOB
                match = find_desired_job(page, desired_locations, log)

                if match is not None:
                    state = JobSearchState.JOB_FOUND
                    _save_screenshot(page, "job-search-match", log)
                    start_alarm()

                    location_label = (
                        ", ".join(desired_locations)
                        if desired_locations
                        else "ANY"
                    )
                    apply_status = (
                        "Apply button is visible."
                        if match["apply_visible"]
                        else "Apply button was not confirmed."
                    )

                    log.warning(
                        "MATCHING WAREHOUSE ASSOCIATE JOB FOUND. URL=%s | %s",
                        match["url"],
                        apply_status,
                    )
                    _notify(
                        "Matching Warehouse Associate job found.\n"
                        f"Location target: {location_label}\n"
                        f"{apply_status}\n\n"
                        "The catcher will now guide the manual application steps "
                        "until the shift-selection page is reached.",
                        title="AMAZON JOB FOUND",
                        priority=5,
                    )
                    print("\nMATCHING WAREHOUSE ASSOCIATE JOB FOUND.")
                    state = JobSearchState.APPLICATION_GUIDE

                    reached_shift_page = guide_user_to_shift_selection(page, log)
                    if reached_shift_page:
                        state = JobSearchState.SHIFT_SELECTION
                        _save_screenshot(page, "job-search-shift-selection", log)
                        _notify(
                            "Amazon shift-selection page reached. "
                            "Automation has stopped before selecting a shift.",
                            title="AMAZON SHIFT PAGE REACHED",
                            priority=5,
                        )
                        stop_reason = "shift-selection page reached"
                    else:
                        stop_reason = "application guide stopped"
                    break

                state = JobSearchState.JOB_SEARCH
                log.info(
                    "No matching Warehouse Associate job found. Checking again in %s seconds.",
                    interval,
                )
                time.sleep(interval)
                open_job_search(page)

        except KeyboardInterrupt:
            stop_reason = "stopped by user"
            log.info("Stopped by user.")
        except Exception as exc:
            state = JobSearchState.ERROR
            stop_reason = "fatal error"
            log.exception("Job Search method stopped with an error: %s", exc)
            start_alarm()
            _save_screenshot(page, "job-search-error", log)
            _notify(
                f"Amazon Job Search catcher stopped with an error:\n{exc}",
                title="Amazon Job Search monitor stopped",
                priority=5,
            )
        finally:
            stop_alarm()
            state = JobSearchState.STOPPED
            log.info("Job Search catcher stopped. Reason=%s | State=%s", stop_reason, state.value)
            log.info("Brave has been left open.")
