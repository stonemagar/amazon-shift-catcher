import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from playwright.sync_api import Page

from .rules import DAY_NAMES, Shift, matches_rules, score_shift


FIELD_LABELS = (
    "Start Date",
    "End Date",
    "Shift",
    "Pay rate",
    "Hours",
    "Location",
)
START_DATE_RE = re.compile(r"^\s*Start\s*Date\b", re.I)
SELECT_THIS_JOB_RE = re.compile(r"^\s*Select\s+this\s+job\s*$", re.I)
ACCEPT_OFFER_RE = re.compile(r"^\s*Accept\s+offer\s*$", re.I)


@dataclass
class ShiftOpportunity:
    index: int
    card: object
    raw_text: str
    start_date: Optional[str]
    end_date: Optional[str]
    shift: Optional[str]
    pay_rate: Optional[str]
    hours: Optional[str]
    location: Optional[str]
    hours_per_week: Optional[float]
    days: list[str]
    start_time: Optional[str]
    end_time: Optional[str]


def _compact(value: str) -> str:
    return " ".join(value.split()).strip()


def normalise_user_time(value: str) -> Optional[str]:
    value = value.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def normalise_user_days(raw: str) -> tuple[list[str], list[str]]:
    """Normalize user-entered day names such as Wed, Thu, Friday."""
    selected = []
    invalid = []

    for token in re.split(r"[,/\s]+", raw.strip()):
        compact = token.strip()
        if not compact:
            continue

        folded = compact.casefold()
        matches = [
            day
            for day in DAY_NAMES
            if len(folded) >= 3 and day.casefold().startswith(folded)
        ]

        if len(matches) != 1:
            invalid.append(compact)
            continue

        day = matches[0]
        if day not in selected:
            selected.append(day)

    return selected, invalid


def _label_pattern(label: str) -> str:
    return re.escape(label).replace(r"\ ", r"\s*")


def _extract_field(raw_text: str, label: str) -> Optional[str]:
    lines = [_compact(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    wanted = re.compile(rf"^{_label_pattern(label)}\s*:?[\s]*(.*)$", re.I)

    for index, line in enumerate(lines):
        match = wanted.match(line)
        if not match:
            continue

        value = _compact(match.group(1))
        if value:
            return value

        if index + 1 < len(lines):
            next_line = lines[index + 1]
            if not any(
                re.match(rf"^{_label_pattern(other)}\s*:?", next_line, re.I)
                for other in FIELD_LABELS
            ):
                return next_line

    compact = _compact(raw_text)
    labels = "|".join(_label_pattern(item) for item in FIELD_LABELS)
    match = re.search(
        rf"{_label_pattern(label)}\s*:?\s*(.*?)"
        rf"(?=\s+(?:{labels})\s*:?|$)",
        compact,
        re.I,
    )
    if match:
        value = _compact(match.group(1))
        return value or None

    return None


def _parse_hours(hours_text: Optional[str], raw_text: str) -> Optional[float]:
    source = hours_text or raw_text
    patterns = (
        r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\s*(?:per\s*week|/\s*week|/\s*wk|weekly)?",
        r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            return float(match.group(1))
    return None


def _parse_days(text: str) -> list[str]:
    result = []
    for day in DAY_NAMES:
        if re.search(rf"\b{day[:3]}\w*\b", text, re.I):
            result.append(day)
    return result


def _parse_time_range(text: str) -> tuple[Optional[str], Optional[str]]:
    patterns = (
        (
            r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s*[-–]\s*"
            r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))"
        ),
        r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalise_user_time(match.group(1)), normalise_user_time(match.group(2))
    return None, None


def parse_shift_opportunity(raw_text: str, index: int = 0, card=None) -> ShiftOpportunity:
    start_date = _extract_field(raw_text, "Start Date")
    end_date = _extract_field(raw_text, "End Date")
    shift_text = _extract_field(raw_text, "Shift")
    pay_rate = _extract_field(raw_text, "Pay rate")
    hours_text = _extract_field(raw_text, "Hours")
    location = _extract_field(raw_text, "Location")

    schedule_source = shift_text or raw_text
    start_time, end_time = _parse_time_range(schedule_source)

    return ShiftOpportunity(
        index=index,
        card=card,
        raw_text=_compact(raw_text),
        start_date=start_date,
        end_date=end_date,
        shift=shift_text,
        pay_rate=pay_rate,
        hours=hours_text,
        location=location,
        hours_per_week=_parse_hours(hours_text, raw_text),
        days=_parse_days(schedule_source),
        start_time=start_time,
        end_time=end_time,
    )


def _shift_card_ancestor(marker):
    best = None
    best_text = ""

    for depth in range(1, 11):
        try:
            ancestor = marker.locator("xpath=" + "/.." * depth)
            raw_text = ancestor.inner_text(timeout=1200)
            compact = _compact(raw_text)
        except Exception:
            continue

        if not compact or len(compact) > 4000:
            continue

        label_count = sum(
            bool(re.search(rf"\b{_label_pattern(label)}\b", compact, re.I))
            for label in FIELD_LABELS
        )
        if label_count < 4:
            continue

        if best is None or len(compact) < len(best_text):
            best = ancestor
            best_text = compact

    if best is not None:
        return best

    return marker


def read_shift_opportunities(page: Page) -> list[ShiftOpportunity]:
    """Read visible shift cards without selecting any card."""
    markers = page.get_by_text(START_DATE_RE)
    opportunities = []
    seen = set()

    try:
        count = markers.count()
    except Exception:
        return opportunities

    for index in range(count):
        marker = markers.nth(index)
        try:
            if not marker.is_visible():
                continue
        except Exception:
            continue

        card = _shift_card_ancestor(marker)
        try:
            raw_text = card.inner_text(timeout=1500)
        except Exception:
            continue

        signature = _compact(raw_text).casefold()
        if not signature or signature in seen:
            continue
        seen.add(signature)

        opportunities.append(
            parse_shift_opportunity(
                raw_text,
                index=len(opportunities),
                card=card,
            )
        )

    return opportunities


def _visible_first(locator):
    try:
        count = locator.count()
    except Exception:
        return None

    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue

    return None


def find_select_this_job_button(page: Page):
    """Return the visible Part 8 'Select this job' control, if present."""
    button = _visible_first(
        page.get_by_role("button", name=SELECT_THIS_JOB_RE)
    )
    if button is not None:
        return button

    return _visible_first(
        page.locator("button, [role='button'], a").filter(
            has_text=SELECT_THIS_JOB_RE
        )
    )


def find_accept_offer_button(page: Page):
    """Return the visible 'Accept offer' control without clicking it."""
    button = _visible_first(
        page.get_by_role("button", name=ACCEPT_OFFER_RE)
    )
    if button is not None:
        return button

    return _visible_first(
        page.locator("button, [role='button'], a").filter(
            has_text=ACCEPT_OFFER_RE
        )
    )


def click_select_this_job(
    page: Page,
    timeout_seconds: float = 8.0,
) -> bool:
    """Click 'Select this job' exactly once and stop before 'Accept offer'."""
    button = find_select_this_job_button(page)
    if button is None:
        return False

    try:
        button.click(timeout=5000)
    except Exception:
        return False

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if find_accept_offer_button(page) is not None:
            return True
        time.sleep(0.15)

    return False


def open_shift_opportunity(
    page: Page,
    opportunity: ShiftOpportunity,
    timeout_seconds: float = 8.0,
) -> bool:
    """Open one matched shift card and stop on the 'Select this job' page.

    This clicks only the shift card itself. It never clicks 'Select this job',
    'Accept offer', or any later application action.
    """
    if opportunity.card is None:
        return False

    try:
        opportunity.card.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass

    try:
        opportunity.card.click(timeout=5000)
    except Exception:
        return False

    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if find_select_this_job_button(page) is not None:
            return True
        time.sleep(0.15)

    return False



def _time_to_minutes(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        hours, minutes = value.split(":", 1)
        return int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return None


def _shift_crosses_midnight(opportunity: ShiftOpportunity) -> bool:
    start = _time_to_minutes(opportunity.start_time)
    end = _time_to_minutes(opportunity.end_time)
    return start is not None and end is not None and end <= start


def _matches_time_preference(
    opportunity: ShiftOpportunity,
    preference: dict,
) -> tuple[bool, Optional[str]]:
    mode = (preference.get("mode") or "any").strip().lower()

    if mode == "any":
        return True, None

    start = _time_to_minutes(opportunity.start_time)
    end = _time_to_minutes(opportunity.end_time)
    if start is None or end is None:
        return False, "shift time could not be read"

    crosses_midnight = end <= start

    if mode == "day":
        if 6 * 60 <= start < 18 * 60 and not crosses_midnight:
            return True, None
        return False, "not a day shift"

    if mode == "night":
        if start >= 18 * 60 or start < 6 * 60 or crosses_midnight:
            return True, None
        return False, "not a night shift"

    if mode == "window":
        earliest = _time_to_minutes(preference.get("earliest_start"))
        latest = _time_to_minutes(preference.get("latest_end"))
        if earliest is None or latest is None:
            return False, "specific time window is incomplete"

        shift_start = start
        shift_end = end + (24 * 60 if crosses_midnight else 0)
        window_end = latest + (24 * 60 if latest <= earliest else 0)

        # For an overnight window such as 20:00-06:00, early-morning starts
        # belong to the next-day portion of the requested window.
        if latest <= earliest and shift_start < earliest:
            shift_start += 24 * 60
            shift_end += 24 * 60

        window_label = (
            f"{preference['earliest_start']} - {preference['latest_end']}"
        )
        shift_label = f"{opportunity.start_time} - {opportunity.end_time}"

        if shift_start > window_end:
            return False, (
                f"shift {shift_label} is outside requested window {window_label}"
            )
        if shift_start < earliest:
            return False, f"starts before requested window {window_label}"
        if shift_end > window_end:
            return False, f"ends after requested window {window_label}"
        return True, None

    return False, f"unknown time preference mode: {mode}"


def apply_time_preference(
    base_rules: dict,
    mode: str,
    earliest_start: Optional[str] = None,
    latest_end: Optional[str] = None,
) -> dict:
    """Return Method 2 rules with runtime time preference overrides applied."""
    rules = dict(base_rules or {})

    # Method 2 handles time preference explicitly below. Disable legacy time
    # restrictions here so config.json does not conflict with the user's choice.
    rules.pop("earliest_start_time", None)
    rules.pop("latest_end_time", None)
    rules["allow_night_shift"] = True

    preference = {"mode": mode}
    if mode == "window":
        preference["earliest_start"] = earliest_start
        preference["latest_end"] = latest_end

    rules["time_preference"] = preference
    return rules


def apply_hours_preference(
    base_rules: dict,
    mode: str,
    *,
    exact_hours: Optional[float] = None,
    max_hours: Optional[float] = None,
    preferred_hours: Optional[float] = None,
) -> dict:
    """Apply the user's Method 2 weekly-hours choice for this run."""
    rules = dict(base_rules or {})
    rules.pop("max_hours_per_week", None)
    rules.pop("preferred_hours_order", None)
    rules.pop("hours_preference", None)

    preference = {"mode": mode}

    if mode == "exact":
        preference["exact_hours"] = float(exact_hours)
        rules["preferred_hours_order"] = [float(exact_hours)]
    elif mode == "max":
        preference["max_hours"] = float(max_hours)
        rules["max_hours_per_week"] = float(max_hours)
    elif mode == "preferred_max":
        preference["preferred_hours"] = float(preferred_hours)
        preference["max_hours"] = float(max_hours)
        rules["max_hours_per_week"] = float(max_hours)
        rules["preferred_hours_order"] = [float(preferred_hours)]
    elif mode != "any":
        raise ValueError(f"Unknown hours preference mode: {mode}")

    rules["hours_preference"] = preference
    return rules


def apply_days_preference(
    base_rules: dict,
    mode: str,
    days: Optional[list[str]] = None,
) -> dict:
    """Apply the user's Method 2 day choice for this run."""
    rules = dict(base_rules or {})
    rules.pop("allowed_days", None)
    rules.pop("blocked_days", None)
    rules.pop("days_preference", None)

    selected = list(days or [])
    preference = {"mode": mode, "days": selected}

    if mode == "allowed":
        rules["allowed_days"] = selected
    elif mode == "blocked":
        rules["blocked_days"] = selected
    elif mode != "any":
        raise ValueError(f"Unknown days preference mode: {mode}")

    rules["days_preference"] = preference
    return rules

def evaluate_shift_opportunity(
    opportunity: ShiftOpportunity,
    rules: dict,
    desired_locations: list[str],
) -> tuple[bool, list[str], int]:
    rule_shift = Shift(
        raw_text=opportunity.raw_text,
        hours_per_week=opportunity.hours_per_week,
        days=opportunity.days,
        start_time=opportunity.start_time,
        end_time=opportunity.end_time,
    )

    matches, reasons = matches_rules(rule_shift, rules)
    reasons = list(reasons)

    hours_preference = rules.get("hours_preference", {})
    if hours_preference.get("mode") == "exact":
        requested = float(hours_preference["exact_hours"])
        if opportunity.hours_per_week is None:
            reason = "weekly hours could not be read"
            if reason not in reasons:
                reasons.append(reason)
        elif abs(opportunity.hours_per_week - requested) > 1e-9:
            reasons.append(
                f"{opportunity.hours_per_week:g}h does not equal requested "
                f"{requested:g}h"
            )

    preference = rules.get("time_preference")
    if preference:
        time_matches, time_reason = _matches_time_preference(
            opportunity,
            preference,
        )
        if not time_matches and time_reason:
            reasons.append(time_reason)

    if desired_locations:
        location_text = opportunity.location or opportunity.raw_text
        haystack = _compact(location_text).casefold()
        if not any(term.casefold() in haystack for term in desired_locations):
            reasons.append(
                "shift location does not match desired location(s): "
                + ", ".join(desired_locations)
            )

    return (matches and not reasons, reasons, score_shift(rule_shift, rules))
