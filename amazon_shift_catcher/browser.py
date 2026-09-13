import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

log = logging.getLogger(__name__)


SECURITY_TEXT = re.compile(
    r"(captcha|verify your identity|verification code|one[- ]time password|"
    r"security check|unusual activity)",
    re.I,
)

SELECT_SHIFT_RE = re.compile(r"^\s*Select Shift\s*$", re.I)
EMPTY_SCHEDULE_TEXT = re.compile(
    r"Sorry,\s*we do not have any schedules matching your preferences",
    re.I,
)
SHIFT_COUNT_TEXT = re.compile(r"Showing\s+\d+\s+of\s+\d+\s+shifts?", re.I)


def page_has_security_challenge(page: Page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    return bool(SECURITY_TEXT.search(text))


def _has_select_shift(page: Page) -> bool:
    try:
        return page.get_by_role("button", name=SELECT_SHIFT_RE).count() > 0
    except Exception:
        return False


def wait_for_manual_login(page: Page, my_jobs_url: str) -> None:
    """Pause automation completely while the user signs in manually.

    The program never types credentials, OTPs, or completes verification.
    """
    if "/selfservice/schedule/" in page.url:
        return

    page.goto(my_jobs_url, wait_until="domcontentloaded")

    if _has_select_shift(page):
        return

    log.warning(
        "Amazon is not logged in. Automation is PAUSED so you can log in manually."
    )
    print("\n" + "=" * 70)
    print("MANUAL LOGIN MODE")
    print("1. Click the Brave window.")
    print("2. Log in to Amazon Jobs manually.")
    print("3. Complete any OTP/security verification manually.")
    print("4. Wait until you can see your Amazon Jobs dashboard.")
    print("5. Return to this terminal and press Enter.")
    print("=" * 70 + "\n")

    input("Press Enter here AFTER you are fully logged in: ")

    if page.is_closed():
        raise RuntimeError("The Brave window was closed during manual login.")

    page.goto(my_jobs_url, wait_until="domcontentloaded")

    if page_has_security_challenge(page):
        raise RuntimeError(
            "Amazon still shows a security/verification step. Complete it manually, then run again."
        )

    if not _has_select_shift(page):
        raise RuntimeError(
            "Login was not detected on the My Jobs page. Make sure you are fully logged in, then run again."
        )

    log.info("Manual Amazon login detected. Continuing automation.")


def reach_shift_page(page: Page, my_jobs_url: str, job_id: str | None = None) -> None:
    if "/selfservice/schedule/" in page.url and "available-schedule" in page.url:
        return

    wait_for_manual_login(page, my_jobs_url)

    if "/selfservice/schedule/" in page.url and "available-schedule" in page.url:
        return

    if not _has_select_shift(page):
        page.goto(my_jobs_url, wait_until="domcontentloaded")

    scope = page
    if job_id:
        body = page.locator("body")
        try:
            job_text = body.get_by_text(job_id, exact=False)
            if job_text.count():
                scope = job_text.first.locator(
                    "xpath=ancestor::*[self::div or self::section][1]"
                )
        except Exception:
            scope = page

    try:
        select_shift = scope.get_by_role("button", name=SELECT_SHIFT_RE)
        if not select_shift.count():
            select_shift = page.get_by_role("button", name=SELECT_SHIFT_RE)
        select_shift.first.click(timeout=10000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Could not find the 'Select Shift' button.") from exc

    page.wait_for_load_state("domcontentloaded")

    if page_has_security_challenge(page):
        raise RuntimeError("Amazon security/verification challenge detected.")

    button = page.get_by_role(
        "button",
        name=re.compile(r"Change schedule and start date", re.I),
    )
    try:
        button.first.click(timeout=10000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Could not find 'Change schedule and start date'.") from exc

    page.wait_for_load_state("domcontentloaded")


def _visible_first(locator):
    try:
        count = locator.count()
    except Exception:
        return None
    for i in range(count):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _visible_last(locator):
    try:
        count = locator.count()
    except Exception:
        return None
    for i in range(count - 1, -1, -1):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _panel_ancestor(anchor, required_text: str):
    """Find the smallest visible ancestor that looks like an Amazon side panel."""
    for depth in range(1, 12):
        try:
            ancestor = anchor.locator("xpath=" + "/.." * depth)
            text = " ".join(ancestor.inner_text(timeout=1000).split())
            if required_text.lower() in text.lower() and "Apply" in text and "Reset" in text:
                return ancestor
        except Exception:
            continue
    return None


def _click_panel_apply(panel, panel_name: str) -> None:
    apply_button = _visible_first(
        panel.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I))
    )
    if apply_button is None:
        # Fallback for UIs where the button role/name is not exposed normally.
        apply_button = _visible_last(panel.get_by_text("Apply", exact=True))
    if apply_button is None:
        raise RuntimeError(f"Could not find the {panel_name} Apply button safely.")
    apply_button.click(timeout=5000)


def wait_for_schedule_settled(page: Page, timeout_ms: int = 8000) -> None:
    """Wait until Amazon is no longer showing the temporary Loading state."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_text = ""
    while time.monotonic() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
            last_text = " ".join(body_text.split())
        except Exception:
            page.wait_for_timeout(250)
            continue

        loading = bool(re.search(r"\bLoading\.\.\.\b", body_text, re.I))
        resolved = bool(
            EMPTY_SCHEDULE_TEXT.search(body_text)
            or SHIFT_COUNT_TEXT.search(body_text)
            or re.search(r"\bHours:\s*\d+(?:\.\d+)?\s*hours?\s*/\s*week\b", body_text, re.I)
        )
        if not loading and resolved:
            return
        page.wait_for_timeout(250)

    log.warning(
        "Amazon schedule page did not reach a recognized settled state within %ss. "
        "Visible text starts: %s",
        timeout_ms / 1000,
        last_text[:300] or "<unavailable>",
    )


def _open_filter_panel(page: Page):
    trigger = _visible_first(page.get_by_text("Filter", exact=True))
    if trigger is None:
        trigger = _visible_first(
            page.get_by_role("button", name=re.compile(r"^\s*Filter\s*$", re.I))
        )
    if trigger is None:
        raise RuntimeError("Could not find Amazon's Filter control.")

    trigger.click(timeout=5000)
    page.wait_for_timeout(250)

    label = _visible_first(page.get_by_text("Desired hours per week", exact=True))
    if label is None:
        raise RuntimeError("Amazon Filter opened, but 'Desired hours per week' was not found.")

    panel = _panel_ancestor(label, "Desired hours per week")
    if panel is None:
        raise RuntimeError("Could not identify Amazon's Filter panel safely.")
    return panel, label


def _reset_filter_to_any_days(panel) -> None:
    """Reset Amazon's Filter first; this restores ANY/default day selection."""
    reset = _visible_first(
        panel.get_by_role("button", name=re.compile(r"^\s*Reset\s*$", re.I))
    )
    if reset is None:
        reset = _visible_first(panel.get_by_text("Reset", exact=True))
    if reset is None:
        raise RuntimeError("Could not find Filter Reset button to restore ANY days.")
    reset.click(timeout=5000)


def _set_desired_hours_by_visible_text(page: Page, panel, max_hours: float) -> str:
    """Set the custom Amazon hours dropdown using the visible option text.

    Amazon's current control is not exposed consistently as a normal combobox,
    so this deliberately uses the visible 'Up to N hours' text seen in the UI.
    """
    expected = f"Up to {max_hours:g} hours"
    hours_pattern = re.compile(r"^\s*Up to\s+\d+(?:\.\d+)?\s+hours?\s*$", re.I)

    current = _visible_first(panel.get_by_text(hours_pattern))
    if current is None:
        # Last-resort DOM text match, still scoped to the Filter panel.
        current = _visible_first(panel.locator("text=/^Up to\\s+\\d+(?:\\.\\d+)?\\s+hours?$/i"))
    if current is None:
        raise RuntimeError("Could not find the visible Desired hours value in Amazon Filter.")

    try:
        current.click(timeout=3000)
    except Exception:
        # Often the displayed text sits inside the clickable custom-select wrapper.
        current.locator("xpath=..").click(timeout=3000)

    page.wait_for_timeout(200)

    option = _visible_last(page.get_by_text(expected, exact=True))
    if option is None:
        option = _visible_last(page.get_by_text(re.compile(rf"^\s*{re.escape(expected)}\s*$", re.I)))
    if option is None:
        raise RuntimeError(f"Could not find '{expected}' after opening Desired hours.")

    option.click(timeout=4000)
    page.wait_for_timeout(200)

    verified = _visible_first(panel.get_by_text(expected, exact=True))
    if verified is None:
        raise RuntimeError(
            f"Desired hours selection did not verify visually as '{expected}'."
        )
    return expected


def _try_set_day_time_preference(page: Page, panel, preference: str) -> bool:
    """Try a literal Amazon DAY option if the current UI exposes one.

    Some Amazon schedule pages expose per-day time ranges rather than a DAY/NIGHT
    preset. In that layout we do not invent a time window; Python's night-shift
    guard remains the second safety layer.
    """
    if not preference:
        return False

    target = _visible_first(
        panel.get_by_text(re.compile(rf"^\s*{re.escape(preference)}\s*$", re.I))
    )
    if target is None:
        return False

    try:
        target.click(timeout=2500)
        page.wait_for_timeout(150)
        return True
    except Exception:
        try:
            target.locator("xpath=..").click(timeout=2500)
            page.wait_for_timeout(150)
            return True
        except Exception:
            return False


def apply_amazon_filter(page: Page, max_hours: float, time_preference: str = "DAY") -> dict:
    """Reapply Amazon Filter after every refresh.

    Current user policy:
      * Desired hours: Up to max_hours
      * Days: ANY (Filter Reset restores all/default days)
      * Time preference: DAY when Amazon exposes a literal DAY preset
    """
    if "/available-schedule/" not in page.url:
        raise RuntimeError("Not on Amazon's available-schedule page.")

    panel, _ = _open_filter_panel(page)
    _reset_filter_to_any_days(panel)
    page.wait_for_timeout(150)

    hours_value = _set_desired_hours_by_visible_text(page, panel, max_hours)
    day_option_applied = _try_set_day_time_preference(page, panel, time_preference)

    _click_panel_apply(panel, "Filter")
    page.wait_for_timeout(350)
    wait_for_schedule_settled(page)

    return {
        "hours": hours_value,
        "days": "ANY",
        "time": time_preference if day_option_applied else "DAY guard in Python",
        "day_option_applied": day_option_applied,
    }


def apply_amazon_sorter(page: Page, sorter_name: str = "Hours – most to least") -> str:
    """Set Amazon's sorter to Hours - most to least using the visible panel text."""
    trigger = _visible_first(
        page.get_by_text(re.compile(r"^\s*Sorter?\s*$", re.I))
    )
    if trigger is None:
        trigger = _visible_first(
            page.get_by_role("button", name=re.compile(r"^\s*Sorter?\s*$", re.I))
        )
    if trigger is None:
        raise RuntimeError("Could not find Amazon's Sort/Sorter control.")

    trigger.click(timeout=5000)
    page.wait_for_timeout(250)

    heading = _visible_first(page.get_by_text("Choose sorter order", exact=True))
    if heading is None:
        heading = _visible_first(page.get_by_text("Sorter", exact=True))
    if heading is None:
        raise RuntimeError("Sorter panel opened, but its contents were not found.")

    panel = _panel_ancestor(heading, "sort")
    if panel is None:
        panel = _panel_ancestor(heading, "Choose sorter order")
    if panel is None:
        raise RuntimeError("Could not identify Amazon's Sorter panel safely.")

    # Amazon uses an en dash in the current UI, but accept hyphen/en-dash/em-dash.
    desired_re = re.compile(r"^\s*Hours\s*[-–—]\s*most\s+to\s+least\s*$", re.I)
    option = _visible_first(panel.get_by_text(desired_re))
    if option is None:
        raise RuntimeError("Could not find 'Hours – most to least' in Amazon Sorter.")

    try:
        option.click(timeout=3000)
    except Exception:
        option.locator("xpath=..").click(timeout=3000)

    page.wait_for_timeout(150)
    _click_panel_apply(panel, "Sorter")
    page.wait_for_timeout(350)
    wait_for_schedule_settled(page)
    return sorter_name


def apply_amazon_preferences(
    page: Page,
    max_hours: float,
    time_preference: str = "DAY",
    sorter_name: str = "Hours – most to least",
) -> dict:
    """Apply Filter then Sorter after each page refresh."""
    filter_status = apply_amazon_filter(page, max_hours, time_preference)
    sorter_value = apply_amazon_sorter(page, sorter_name)
    filter_status["sorter"] = sorter_value
    return filter_status


def get_apply_buttons(page: Page):
    return page.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I))


def _candidate_card_text(button) -> str:
    best = ""
    for depth in range(1, 8):
        try:
            ancestor = button.locator("xpath=" + "/.." * depth)
            text = " ".join(ancestor.inner_text(timeout=1500).split())
        except Exception:
            continue

        if not text:
            continue

        looks_like_shift = bool(
            re.search(
                r"(per\s*week|Schedule\s*\(|Start date|Supported languages)",
                text,
                re.I,
            )
        )
        if looks_like_shift:
            if not best or len(text) < len(best):
                best = text

    if best:
        return best

    try:
        return " ".join(button.locator("xpath=..").inner_text().split())
    except Exception:
        return ""


def read_shift_cards(page: Page) -> list[dict]:
    buttons = get_apply_buttons(page)
    cards = []
    for i in range(buttons.count()):
        button = buttons.nth(i)
        try:
            if not button.is_visible():
                continue
        except Exception:
            continue

        text = _candidate_card_text(button)
        # Do not treat Filter/Sorter Apply buttons as shift actions.
        if not re.search(r"(hours?\s*/\s*week|hours?\s*per\s*week|Start date|Shift:)", text, re.I):
            continue

        cards.append({
            "index": i,
            "button": button,
            "text": text,
        })
    return cards


def save_screenshot(page: Page, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
