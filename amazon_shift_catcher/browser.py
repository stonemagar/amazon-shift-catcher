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


def page_has_security_challenge(page: Page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    return bool(SECURITY_TEXT.search(text))


def reach_shift_page(page: Page, my_jobs_url: str, job_id: str | None = None) -> None:
    if "/selfservice/schedule/" in page.url and "available-schedule" in page.url:
        return

    page.goto(my_jobs_url, wait_until="domcontentloaded")

    if page_has_security_challenge(page):
        raise RuntimeError("Amazon security/verification challenge detected.")

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
        select_shift = scope.get_by_role(
            "button", name=re.compile(r"^\s*Select Shift\s*$", re.I)
        )
        if not select_shift.count():
            select_shift = page.get_by_role(
                "button", name=re.compile(r"^\s*Select Shift\s*$", re.I)
            )
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

        cards.append({
            "index": i,
            "button": button,
            "text": _candidate_card_text(button),
        })
    return cards


def save_screenshot(page: Page, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
