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
    click_application_stage,
    click_confirm_selection,
    detect_application_stage,
    find_apply_button,
    find_open_application_tab,
    find_submit_application_button,
    job_candidate_key,
    location_match_rank,
    normalise_location_terms,
    open_job_candidate,
    open_job_search,
    page_is_blocked,
    read_job_detail_text,
    read_reported_job_count,
    read_warehouse_job_cards,
    reset_job_search_scroll,
    return_to_job_search,
    scroll_job_search_results,
    text_matches_location,
    wait_for_manual_login,
)
from .job_search_shifts import (
    apply_days_preference,
    apply_hours_preference,
    apply_time_preference,
    click_select_this_job,
    evaluate_shift_opportunity,
    find_accept_offer_button,
    find_select_this_job_button,
    focus_accept_offer_button,
    normalise_user_days,
    normalise_user_time,
    open_shift_opportunity,
    read_shift_opportunities,
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
    SHIFT_REVIEW = "SHIFT_REVIEW"
    SHIFT_DETAILS = "SHIFT_DETAILS"
    JOB_SELECTED = "JOB_SELECTED"
    APPOINTMENT_SELECTION = "APPOINTMENT_SELECTION"
    APPLICATION_REVIEW = "APPLICATION_REVIEW"
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



def choose_location_strategy(desired_locations: list[str]) -> str:
    """Choose how Method 2 handles more than one requested location."""
    if len(desired_locations) <= 1:
        return "first"

    print("\n" + "=" * 64)
    print("MULTI-LOCATION SELECTION")
    print("1 = Fastest: use the first matching location found")
    print("2 = Priority: prefer locations in the order you entered them")
    print("=" * 64)

    while True:
        choice = input("Select location strategy [1-2]: ").strip()
        if choice == "1":
            print("Location strategy: FIRST MATCH")
            return "first"
        if choice == "2":
            print(
                "Location strategy: PRIORITY ORDER ("
                + " > ".join(desired_locations)
                + ")"
            )
            return "priority"
        print("Please enter 1 or 2.")


def _read_positive_hours(prompt: str) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        print("Please enter a positive number, for example 20 or 16.5.")


def choose_hours_preference(base_rules: dict) -> dict:
    print("\n" + "=" * 64)
    print("WEEKLY HOURS PREFERENCE")
    print("1 = Any hours")
    print("2 = Exact hours")
    print("3 = Maximum hours")
    print("4 = Preferred hours + maximum")
    print("=" * 64)

    while True:
        choice = input("Select hours preference [1-4]: ").strip()

        if choice == "1":
            print("Hours preference: ANY")
            return apply_hours_preference(base_rules, "any")

        if choice == "2":
            exact = _read_positive_hours("Exact weekly hours: ")
            print(f"Hours preference: EXACTLY {exact:g}h")
            return apply_hours_preference(
                base_rules,
                "exact",
                exact_hours=exact,
            )

        if choice == "3":
            maximum = _read_positive_hours("Maximum weekly hours: ")
            print(f"Hours preference: MAXIMUM {maximum:g}h")
            return apply_hours_preference(
                base_rules,
                "max",
                max_hours=maximum,
            )

        if choice == "4":
            preferred = _read_positive_hours("Preferred weekly hours: ")
            maximum = _read_positive_hours("Maximum weekly hours: ")
            if preferred > maximum:
                print("Preferred hours cannot exceed maximum hours. Try again.")
                continue
            print(
                f"Hours preference: PREFER {preferred:g}h, "
                f"MAXIMUM {maximum:g}h"
            )
            return apply_hours_preference(
                base_rules,
                "preferred_max",
                preferred_hours=preferred,
                max_hours=maximum,
            )

        print("Please enter 1, 2, 3, or 4.")


def choose_days_preference(base_rules: dict) -> dict:
    print("\n" + "=" * 64)
    print("DAY PREFERENCE")
    print("1 = Any days")
    print("2 = Choose allowed days")
    print("3 = Choose blocked days")
    print("=" * 64)

    while True:
        choice = input("Select day preference [1-3]: ").strip()

        if choice == "1":
            print("Day preference: ANY")
            return apply_days_preference(base_rules, "any")

        if choice in {"2", "3"}:
            while True:
                raw = input(
                    "Enter days, e.g. Wed, Thu, Fri, Sat, Sun: "
                ).strip()
                days, invalid = normalise_user_days(raw)
                if invalid:
                    print("Invalid day(s): " + ", ".join(invalid))
                    continue
                if not days:
                    print("Please enter at least one day.")
                    continue

                if choice == "2":
                    print("Allowed days: " + ", ".join(days))
                    return apply_days_preference(
                        base_rules,
                        "allowed",
                        days,
                    )

                print("Blocked days: " + ", ".join(days))
                return apply_days_preference(
                    base_rules,
                    "blocked",
                    days,
                )

        print("Please enter 1, 2, or 3.")


def choose_time_preference(base_rules: dict) -> dict:
    """Ask how the user wants Method 2 to interpret shift time."""
    print("\n" + "=" * 64)
    print("SHIFT TIME PREFERENCE")
    print("1 = Any time")
    print("2 = Day shift   (starts 06:00-17:59 and does not cross midnight)")
    print("3 = Night shift (starts 18:00-05:59 or crosses midnight)")
    print("4 = Specific time window")
    print("=" * 64)

    while True:
        choice = input("Select time preference [1-4]: ").strip()

        if choice == "1":
            print("Time preference: ANY")
            return apply_time_preference(base_rules, "any")

        if choice == "2":
            print("Time preference: DAY SHIFT")
            return apply_time_preference(base_rules, "day")

        if choice == "3":
            print("Time preference: NIGHT SHIFT")
            return apply_time_preference(base_rules, "night")

        if choice == "4":
            while True:
                earliest_raw = input("Earliest start time (HH:MM): ").strip()
                latest_raw = input("Latest end time (HH:MM): ").strip()
                earliest = normalise_user_time(earliest_raw)
                latest = normalise_user_time(latest_raw)

                if earliest and latest:
                    print(
                        "Time preference: SPECIFIC WINDOW "
                        f"{earliest} - {latest}"
                    )
                    return apply_time_preference(
                        base_rules,
                        "window",
                        earliest_start=earliest,
                        latest_end=latest,
                    )

                print("Invalid time. Example: 09:00, 13:30, 9 AM, 10:30 PM.")

        print("Please enter 1, 2, 3, or 4.")


def print_filter_summary(
    desired_locations: list[str],
    rules: dict,
    location_strategy: str = "first",
) -> None:
    hours = rules.get("hours_preference", {"mode": "any"})
    days = rules.get("days_preference", {"mode": "any"})
    time_pref = rules.get("time_preference", {"mode": "any"})

    print("\n" + "=" * 64)
    print("SEARCH FILTER SUMMARY")
    print(
        "Location: "
        + (", ".join(desired_locations) if desired_locations else "ANY")
    )

    if len(desired_locations) > 1:
        if location_strategy == "priority":
            print("Location selection: PRIORITY ORDER")
        else:
            print("Location selection: FIRST MATCH")

    hour_mode = hours.get("mode", "any")
    if hour_mode == "exact":
        print(f"Hours: EXACTLY {hours['exact_hours']:g}h")
    elif hour_mode == "max":
        print(f"Hours: MAXIMUM {hours['max_hours']:g}h")
    elif hour_mode == "preferred_max":
        print(
            f"Hours: PREFER {hours['preferred_hours']:g}h, "
            f"MAXIMUM {hours['max_hours']:g}h"
        )
    else:
        print("Hours: ANY")

    day_mode = days.get("mode", "any")
    if day_mode == "allowed":
        print("Allowed days: " + ", ".join(days.get("days", [])))
    elif day_mode == "blocked":
        print("Blocked days: " + ", ".join(days.get("days", [])))
    else:
        print("Days: ANY")

    time_mode = time_pref.get("mode", "any")
    if time_mode == "day":
        print("Time: DAY SHIFT")
    elif time_mode == "night":
        print("Time: NIGHT SHIFT")
    elif time_mode == "window":
        print(
            "Time: "
            f"{time_pref.get('earliest_start')} - "
            f"{time_pref.get('latest_end')}"
        )
    else:
        print("Time: ANY")

    print("=" * 64)


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


def _find_candidate_by_key(page, wanted_key: str):
    """Re-find a lazy-loaded job card by its stable key."""
    reset_job_search_scroll(page)

    for _step in range(40):
        for candidate in read_warehouse_job_cards(page):
            if job_candidate_key(candidate) == wanted_key:
                return candidate

        if not scroll_job_search_results(page):
            time.sleep(0.35)
            for candidate in read_warehouse_job_cards(page):
                if job_candidate_key(candidate) == wanted_key:
                    return candidate
            break

    return None


def _open_ranked_job_match(page, match: dict, log):
    candidate = _find_candidate_by_key(page, match["key"])
    if candidate is None:
        log.warning(
            "A preferred matching job was discovered, but its card could not "
            "be re-located after the full result scan."
        )
        return None

    log.info(
        "Opening selected location match: %s",
        candidate.location or candidate.text[:220],
    )
    open_job_candidate(page, candidate)

    if page_is_blocked(page):
        raise RuntimeError("Amazon returned a 403 / request-blocked page.")

    detail_text = read_job_detail_text(page)
    apply_button = find_apply_button(page)
    if apply_button is None:
        log.warning(
            "Location matched, but no visible Apply button was found "
            "on the details page."
        )

    return {
        "card_text": candidate.text,
        "detail_text": detail_text or match.get("detail_text", ""),
        "apply_visible": apply_button is not None,
        "url": page.url,
    }


def find_desired_job(
    page,
    desired_locations: list[str],
    log,
    location_strategy: str = "first",
):
    """Scan Warehouse Associate cards and choose a matching location.

    'first' preserves the fastest behavior: the first matching card wins.
    'priority' scans the result set and prefers locations in the exact order
    entered by the user. A rank-0 match can be opened immediately because no
    lower-priority result can beat it.
    """
    reported_total = read_reported_job_count(page)
    if reported_total is not None:
        log.info(
            "Amazon reports %s job(s) found. Scanning the full result page.",
            reported_total,
        )

    reset_job_search_scroll(page)

    seen_keys = set()
    scan_steps = 0
    max_scan_steps = 40
    no_new_batches = 0
    best_match = None

    while scan_steps < max_scan_steps:
        cards = read_warehouse_job_cards(page)
        new_cards = []

        for candidate in cards:
            key = job_candidate_key(candidate)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_cards.append(candidate)

        if new_cards:
            no_new_batches = 0
            visible_locations = [
                candidate.location
                for candidate in new_cards
                if candidate.location
            ]
            added_locations = save_unique_locations(
                LOCATION_STORE,
                visible_locations,
            )
            if added_locations:
                log.info(
                    "Saved %s new unique card location(s): %s",
                    len(added_locations),
                    ", ".join(added_locations),
                )

            log.info(
                "Discovered %s new Warehouse Associate card(s); "
                "%s unique card(s) checked/discovered so far.",
                len(new_cards),
                len(seen_keys),
            )

            restart_scan = False

            for offset, candidate in enumerate(new_cards):
                card_number = len(seen_keys) - len(new_cards) + offset + 1
                total_label = (
                    str(reported_total)
                    if reported_total is not None
                    else "?"
                )
                log.info(
                    "Checking Warehouse Associate card %s/%s: %s",
                    card_number,
                    total_label,
                    candidate.text[:350] or "<no readable card text>",
                )

                location_text = candidate.location or candidate.text
                match_rank = location_match_rank(
                    location_text,
                    desired_locations,
                )

                if match_rank is not None:
                    if location_strategy != "priority" or match_rank == 0:
                        log.info(
                            "Desired location matched directly on the result "
                            "card. Opening this match."
                        )
                        open_job_candidate(page, candidate)

                        if page_is_blocked(page):
                            raise RuntimeError(
                                "Amazon returned a 403 / request-blocked page."
                            )

                        detail_text = read_job_detail_text(page)
                        apply_button = find_apply_button(page)
                        if apply_button is None:
                            log.warning(
                                "Location matched, but no visible Apply button "
                                "was found on the details page."
                            )
                        return {
                            "card_text": candidate.text,
                            "detail_text": detail_text,
                            "apply_visible": apply_button is not None,
                            "url": page.url,
                        }

                    if (
                        best_match is None
                        or match_rank < best_match["rank"]
                    ):
                        best_match = {
                            "key": job_candidate_key(candidate),
                            "rank": match_rank,
                            "detail_text": "",
                        }
                        log.info(
                            "Stored current best location match: %s "
                            "(priority %s). Continuing scan for a higher "
                            "priority location.",
                            candidate.location or candidate.text[:120],
                            match_rank + 1,
                        )
                    continue

                if not card_requires_detail_location_check(candidate.text):
                    log.info(
                        "Visible card location does not match. "
                        "Skipping this card without opening it."
                    )
                    continue

                log.info(
                    "Card says multiple on site locations available. "
                    "Opening it to check the detailed location."
                )
                key = job_candidate_key(candidate)
                open_job_candidate(page, candidate)

                if page_is_blocked(page):
                    raise RuntimeError(
                        "Amazon returned a 403 / request-blocked page."
                    )

                detail_text = read_job_detail_text(page)
                detail_rank = location_match_rank(
                    detail_text,
                    desired_locations,
                )

                if detail_rank is not None:
                    if location_strategy != "priority" or detail_rank == 0:
                        apply_button = find_apply_button(page)
                        if apply_button is None:
                            log.warning(
                                "Detailed location matched, but no visible Apply "
                                "button was found."
                            )
                        return {
                            "card_text": candidate.text,
                            "detail_text": detail_text,
                            "apply_visible": apply_button is not None,
                            "url": page.url,
                        }

                    if (
                        best_match is None
                        or detail_rank < best_match["rank"]
                    ):
                        best_match = {
                            "key": key,
                            "rank": detail_rank,
                            "detail_text": detail_text,
                        }
                        log.info(
                            "Stored current best detailed location match at "
                            "priority %s. Continuing scan.",
                            detail_rank + 1,
                        )

                log.info(
                    "Returning to search results and continuing the full scan."
                )
                return_to_job_search(page)
                reset_job_search_scroll(page)
                restart_scan = True
                break

            if restart_scan:
                scan_steps += 1
                continue

        else:
            no_new_batches += 1

        moved = scroll_job_search_results(page)
        scan_steps += 1

        if moved:
            continue

        time.sleep(0.5)
        final_cards = read_warehouse_job_cards(page)
        final_new = [
            candidate
            for candidate in final_cards
            if job_candidate_key(candidate) not in seen_keys
        ]
        if final_new:
            continue

        if no_new_batches >= 1 or not moved:
            break

    log.info(
        "Finished full result-page scan. %s unique Warehouse Associate card(s) "
        "were discovered and checked.",
        len(seen_keys),
    )

    if best_match is not None:
        selected_location = desired_locations[best_match["rank"]]
        log.info(
            "Full scan complete. Choosing highest-priority discovered "
            "location match: %s",
            selected_location,
        )
        return _open_ranked_job_match(page, best_match, log)

    if reported_total is not None and len(seen_keys) < reported_total:
        log.warning(
            "Amazon reports %s total job result(s), but %s Warehouse Associate "
            "card(s) were discovered. The reported total can include other role "
            "titles; if more Warehouse Associate cards are visibly present, send "
            "the card titles or a screenshot so the title/card detector can be "
            "adjusted.",
            reported_total,
            len(seen_keys),
        )
    return None


def advance_to_shift_selection(page, log):
    """Automatically follow Apply -> Next -> Start Application -> optional Continue.

    Each stage is clicked at most once. If Amazon opens a new tab, control moves to
    that tab and the program waits for a different stage before clicking again.
    Security, OTP, CAPTCHA, identity and other verification steps remain manual.
    """
    expected_order = ["APPLY", "NEXT", "START_APPLICATION", "CONTINUE"]
    clicked_actions = set()
    last_stage = None
    idle_started = time.monotonic()

    print("\nAUTOMATIC APPLICATION FLOW")
    print("Following: Apply -> Next -> Start Application -> optional Continue")
    print("Each application button will be clicked only once.")
    print("Security/verification steps will NOT be bypassed.\n")

    while not page.is_closed():
        if page_is_blocked(page):
            raise RuntimeError("Amazon returned a 403 / request-blocked page.")

        # After Apply, Amazon opens the application flow in a new tab.
        # Re-scan the browser context on every loop so we can recover even if the
        # new tab appeared just after the click helper returned.
        if any(stage == "APPLY" for stage, _url in clicked_actions):
            application_tab = find_open_application_tab(page)
            if application_tab is not None and application_tab is not page:
                log.info(
                    "Detected Amazon application tab. Switching control to: %s",
                    application_tab.url,
                )
                page = application_tab
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                last_stage = None
                idle_started = time.monotonic()

        stage = detect_application_stage(page)

        if stage != last_stage:
            last_stage = stage
            idle_started = time.monotonic()
            log.info("Application stage detected: %s | URL=%s", stage, page.url)

        if stage == "SHIFT_SELECTION":
            log.info("Shift-selection page reached.")
            return page

        if stage == "APPOINTMENT_SELECTION":
            log.info("Pre-hire appointment selection page reached.")
            return page

        if stage == "SECURITY_CHECK":
            raise RuntimeError(
                "Amazon security/verification step detected. "
                "Complete it manually, then run the catcher again."
            )

        if stage == "POST_SHIFT":
            log.warning(
                "Amazon resumed this application after shift selection. "
                "A scheduleId is already attached, so Method 2 will not click "
                "the later Additional Information 'Next' button."
            )
            print(
                "\nAPPLICATION ALREADY PROGRESSED PAST SHIFT SELECTION. "
                "A schedule is already attached to this application."
            )
            return None

        if stage in expected_order:
            action_key = (stage, page.url)

            if action_key in clicked_actions:
                # Guard against re-clicking the same control while one page is
                # still loading. The same label on a different URL is a
                # different application step.
                if time.monotonic() - idle_started > 15:
                    raise RuntimeError(
                        f"Application stage {stage} did not advance after one click. "
                        f"Current URL: {page.url}"
                    )
                time.sleep(0.4)
                continue

            clicked_actions.add(action_key)
            log.info("Automatically clicking %s once.", stage)

            next_page = click_application_stage(page, stage)
            if next_page is None:
                raise RuntimeError(f"Could not click expected application stage: {stage}")

            if next_page is not page:
                log.info(
                    "Application flow opened a new tab. Switching control to: %s",
                    next_page.url,
                )
                page = next_page

            # Wait for the new tab/page to expose a different stage before any
            # further application click is allowed.
            idle_started = time.monotonic()
            time.sleep(0.6)
            continue

        if stage == "UNKNOWN" and time.monotonic() - idle_started > 15:
            raise RuntimeError(
                "Application flow reached an unknown page and did not advance "
                "for 15 seconds."
            )

        time.sleep(0.4)

    return None


def review_shift_opportunities(
    page,
    rules: dict,
    desired_locations: list[str],
    log,
):
    """Read and evaluate visible shift cards without selecting any shift."""
    opportunities = read_shift_opportunities(page)

    if not opportunities:
        log.warning("Shift-selection page detected, but no readable shift cards were found.")
        print("No readable shift cards found yet.")
        return []

    log.info("Found %s visible shift card(s).", len(opportunities))
    evaluated = []

    for opportunity in opportunities:
        is_match, reasons, score = evaluate_shift_opportunity(
            opportunity,
            rules,
            desired_locations,
        )
        evaluated.append((opportunity, is_match, reasons, score))

        status = "MATCH" if is_match else "NO MATCH"
        log.info(
            "Shift %s | %s | start=%s | end=%s | shift=%s | pay=%s | "
            "hours=%s | location=%s | score=%s | reasons=%s",
            opportunity.index + 1,
            status,
            opportunity.start_date or "?",
            opportunity.end_date or "?",
            opportunity.shift or "?",
            opportunity.pay_rate or "?",
            opportunity.hours or "?",
            opportunity.location or "?",
            score,
            "; ".join(reasons) if reasons else "none",
        )

    matching = [item for item in evaluated if item[1]]
    matching.sort(key=lambda item: item[3], reverse=True)

    print("\nSHIFT REVIEW")
    print(f"Visible shift cards: {len(evaluated)}")
    print(f"Matching shift cards: {len(matching)}")

    for opportunity, is_match, reasons, score in evaluated:
        print(
            f"#{opportunity.index + 1} "
            f"{'MATCH' if is_match else 'NO MATCH'} | "
            f"Start: {opportunity.start_date or '?'} | "
            f"End: {opportunity.end_date or '?'} | "
            f"Shift: {opportunity.shift or '?'} | "
            f"Hours: {opportunity.hours or '?'} | "
            f"Location: {opportunity.location or '?'}"
        )
        if reasons:
            print("  Reasons: " + "; ".join(reasons))

    if matching:
        best = matching[0][0]
        print(
            "\nBest matching visible shift: "
            f"#{best.index + 1} | "
            f"{best.shift or '?'} | {best.hours or '?'} | {best.location or '?'}"
        )
        print("Best match is ready to open.")
    else:
        print("\nNo visible shift currently matches all configured rules.")

    return evaluated


def run() -> None:
    load_dotenv(ROOT / ".env")
    config = load_config()
    desired_locations = choose_desired_locations()
    location_strategy = choose_location_strategy(desired_locations)

    runtime_rules = choose_hours_preference(config.get("rules", {}))
    runtime_rules = choose_days_preference(runtime_rules)
    runtime_rules = choose_time_preference(runtime_rules)
    print_filter_summary(
        desired_locations,
        runtime_rules,
        location_strategy,
    )

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
    log.info("Location strategy=%s", location_strategy)
    log.info("Runtime hours preference=%s", runtime_rules.get("hours_preference"))
    log.info("Runtime days preference=%s", runtime_rules.get("days_preference"))
    log.info("Runtime time preference=%s", runtime_rules.get("time_preference"))
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
                match = find_desired_job(
                    page,
                    desired_locations,
                    log,
                    location_strategy,
                )

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
                        "The catcher will now follow Apply -> Next -> Start Application "
                        "-> optional Continue automatically.",
                        title="AMAZON JOB FOUND",
                        priority=5,
                    )
                    print("\nMATCHING WAREHOUSE ASSOCIATE JOB FOUND.")
                    state = JobSearchState.APPLICATION_GUIDE

                    application_page = advance_to_shift_selection(page, log)
                    if application_page is not None:
                        page = application_page
                        destination_stage = detect_application_stage(page)

                        if destination_stage == "APPOINTMENT_SELECTION":
                            state = JobSearchState.APPOINTMENT_SELECTION
                            _save_screenshot(
                                page,
                                "job-search-prehire-appointment",
                                log,
                            )
                            log.warning(
                                "Pre-hire appointment page reached. "
                                "Automatically clicking 'Confirm Selection' once "
                                "without changing the displayed appointment."
                            )

                            confirmed = click_confirm_selection(page)
                            if not confirmed:
                                raise RuntimeError(
                                    "Could not click 'Confirm Selection' or "
                                    "confirm the Submit application page."
                                )

                            state = JobSearchState.APPLICATION_REVIEW
                            _save_screenshot(
                                page,
                                "job-search-application-review",
                                log,
                            )

                            if find_submit_application_button(page) is None:
                                raise RuntimeError(
                                    "Appointment was confirmed, but the "
                                    "'Submit application' button was not found."
                                )

                            print(
                                "\n'CONFIRM SELECTION' CLICKED AUTOMATICALLY."
                            )
                            print(
                                "The program has stopped before 'Submit application'."
                            )
                            _notify(
                                "Amazon appointment selection was confirmed "
                                "automatically. The program stopped before "
                                "'Submit application'.",
                                title="AMAZON APPOINTMENT CONFIRMED",
                                priority=5,
                            )
                            stop_reason = (
                                "Confirm Selection clicked; "
                                "Submit application awaiting user"
                            )
                            break

                        state = JobSearchState.SHIFT_SELECTION
                        _save_screenshot(page, "job-search-shift-selection", log)

                        state = JobSearchState.SHIFT_REVIEW
                        evaluated = review_shift_opportunities(
                            page,
                            runtime_rules,
                            desired_locations,
                            log,
                        )

                        matching = [item for item in evaluated if item[1]]
                        matching.sort(key=lambda item: item[3], reverse=True)

                        if matching:
                            best = matching[0][0]
                            log.warning(
                                "Opening best matching shift card #%s. "
                                "No offer/acceptance button will be clicked.",
                                best.index + 1,
                            )

                            opened = open_shift_opportunity(page, best)
                            if not opened:
                                raise RuntimeError(
                                    "Best matching shift was found, but its card "
                                    "did not open the 'Select this job' page."
                                )

                            state = JobSearchState.SHIFT_DETAILS
                            _save_screenshot(
                                page,
                                "job-search-best-shift",
                                log,
                            )

                            if find_select_this_job_button(page) is None:
                                raise RuntimeError(
                                    "Best shift page opened, but the "
                                    "'Select this job' button was not found."
                                )

                            log.warning(
                                "Automatically clicking 'Select this job' once. "
                                "The program will stop before 'Accept offer'."
                            )
                            selected = click_select_this_job(page)
                            if not selected:
                                raise RuntimeError(
                                    "'Select this job' was clicked, but the "
                                    "'Accept offer' page was not confirmed."
                                )

                            state = JobSearchState.JOB_SELECTED
                            _save_screenshot(
                                page,
                                "job-search-after-select-this-job",
                                log,
                            )

                            if find_accept_offer_button(page) is None:
                                raise RuntimeError(
                                    "Job selection advanced, but the "
                                    "'Accept offer' button was not found."
                                )

                            if not focus_accept_offer_button(page):
                                raise RuntimeError(
                                    "The 'Accept offer' button was found, but "
                                    "could not be brought into view."
                                )

                            print(
                                "\n'SELECT THIS JOB' CLICKED AUTOMATICALLY. "
                                "'Accept offer' has been brought into view and focused."
                            )
                            _notify(
                                "Best matching Amazon shift was selected with "
                                "'Select this job'. The program stopped before "
                                "'Accept offer'.",
                                title="AMAZON SHIFT SELECTED",
                                priority=5,
                            )
                            stop_reason = (
                                "Select this job clicked; "
                                "Accept offer awaiting user"
                            )
                        else:
                            _notify(
                                "Amazon shift-selection page reached and visible "
                                "shift cards were reviewed. No shift currently "
                                "matches all selected filters.",
                                title="AMAZON SHIFT REVIEW COMPLETE",
                                priority=5,
                            )
                            stop_reason = "shift review complete; no matching shift"
                    else:
                        stop_reason = "application flow stopped"
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
