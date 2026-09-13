import logging
import re
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

log = logging.getLogger(__name__)


SECURITY_TEXT = re.compile(
    r"(captcha|verify your identity|verification code|one[- ]time password|"
    r"security check|unusual activity)",
    re.I,
)

SELECT_SHIFT_RE = re.compile(r"^\s*Select Shift\s*$", re.I)


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
    for i in range(locator.count()):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _panel_ancestor(anchor, required_text: str):
    """Find the smallest visible ancestor that looks like the open side panel."""
    for depth in range(1, 9):
        try:
            ancestor = anchor.locator("xpath=" + "/.." * depth)
            text = " ".join(ancestor.inner_text(timeout=1000).split())
            if required_text.lower() in text.lower() and "Apply" in text and "Reset" in text:
                return ancestor
        except Exception:
            continue
    return None


def ensure_desired_hours_filter(page: Page, max_hours: float) -> str:
    """Set and verify Amazon's 'Desired hours per week' filter.

    Returns the verified display value, e.g. 'Up to 20 hours'. Raises if the
    control cannot be found or verified, so monitoring does not silently run
    with the wrong hours filter.
    """
    if "/available-schedule/" not in page.url:
        raise RuntimeError("Not on Amazon's available-schedule page.")

    expected = f"Up to {max_hours:g} hours"

    filter_trigger = _visible_first(page.get_by_text("Filter", exact=True))
    if filter_trigger is None:
        raise RuntimeError("Could not find Amazon's Filter control.")
    filter_trigger.click(timeout=5000)
    page.wait_for_timeout(300)

    label = _visible_first(page.get_by_text("Desired hours per week", exact=True))
    if label is None:
        raise RuntimeError("Could not find 'Desired hours per week' in Amazon Filter.")

    container = label.locator("xpath=..")
    combo = _visible_first(container.get_by_role("combobox"))
    if combo is None:
        combo = _visible_first(container.locator("select"))
    if combo is None:
        raise RuntimeError("Could not find the Desired hours dropdown.")

    # Native select first; Amazon may render this as a normal <select>.
    set_ok = False
    try:
        combo.select_option(label=expected)
        set_ok = True
    except Exception:
        pass

    # Fallback for a custom dropdown.
    if not set_ok:
        combo.click(timeout=3000)
        option = _visible_first(page.get_by_role("option", name=expected, exact=True))
        if option is None:
            option = _visible_first(page.get_by_text(expected, exact=True))
        if option is None:
            raise RuntimeError(f"Could not find '{expected}' in Desired hours dropdown.")
        option.click(timeout=3000)

    # Read the chosen value before applying.
    selected = ""
    try:
        selected = combo.input_value().strip()
    except Exception:
        pass
    if expected.lower() not in selected.lower():
        try:
            checked = combo.locator("option:checked")
            if checked.count():
                selected = checked.first.inner_text().strip()
        except Exception:
            pass
    if expected.lower() not in selected.lower():
        try:
            selected = " ".join(combo.inner_text().split())
        except Exception:
            pass

    # Scope Apply to the Filter panel so it is never confused with a shift action.
    panel = _panel_ancestor(label, "Desired hours per week")
    if panel is None:
        raise RuntimeError("Could not identify the Amazon Filter panel safely.")
    apply_button = _visible_first(
        panel.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I))
    )
    if apply_button is None:
        raise RuntimeError("Could not find the Filter Apply button safely.")
    apply_button.click(timeout=5000)
    page.wait_for_timeout(800)

    # Re-open and verify the setting really persisted.
    filter_trigger = _visible_first(page.get_by_text("Filter", exact=True))
    if filter_trigger is None:
        raise RuntimeError("Could not re-open Filter to verify Desired hours.")
    filter_trigger.click(timeout=5000)
    page.wait_for_timeout(300)

    label = _visible_first(page.get_by_text("Desired hours per week", exact=True))
    if label is None:
        raise RuntimeError("Could not verify Desired hours after applying filter.")
    container = label.locator("xpath=..")
    combo = _visible_first(container.get_by_role("combobox"))
    if combo is None:
        combo = _visible_first(container.locator("select"))
    if combo is None:
        raise RuntimeError("Could not re-read Desired hours dropdown.")

    verified = ""
    try:
        verified = combo.input_value().strip()
    except Exception:
        pass
    if expected.lower() not in verified.lower():
        try:
            checked = combo.locator("option:checked")
            if checked.count():
                verified = checked.first.inner_text().strip()
        except Exception:
            pass
    if expected.lower() not in verified.lower():
        try:
            verified = " ".join(combo.inner_text().split())
        except Exception:
            pass

    if expected.lower() not in verified.lower():
        raise RuntimeError(
            f"Amazon Desired hours filter verification failed. Expected '{expected}', "
            f"but read '{verified or '<unknown>'}'."
        )

    # Close the panel without changing anything further.
    close_button = _visible_first(panel.get_by_role("button", name=re.compile(r"close", re.I)))
    if close_button is not None:
        try:
            close_button.click(timeout=2000)
        except Exception:
            pass
    else:
        try:
            filter_trigger.click(timeout=2000)
        except Exception:
            pass

    return expected


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
