import re
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


def _normalise_time(value: str) -> Optional[str]:
    value = value.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


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
            return _normalise_time(match.group(1)), _normalise_time(match.group(2))
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

    if desired_locations:
        location_text = opportunity.location or opportunity.raw_text
        haystack = _compact(location_text).casefold()
        if not any(term.casefold() in haystack for term in desired_locations):
            reasons.append(
                "shift location does not match desired location(s): "
                + ", ".join(desired_locations)
            )

    return (matches and not reasons, reasons, score_shift(rule_shift, rules))
