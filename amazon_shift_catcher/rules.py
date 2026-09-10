import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


DAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday"
)


@dataclass
class Shift:
    raw_text: str
    hours_per_week: Optional[float]
    days: list[str]
    start_time: Optional[str]
    end_time: Optional[str]


def _normalise_time(value: str) -> Optional[str]:
    value = value.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def parse_shift(text: str) -> Shift:
    compact = " ".join(text.split())

    hours = None
    hour_patterns = [
        r"Schedule\s*\(\s*(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*per\s*week\s*\)",
        r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*per\s*week",
        r"(\d+(?:\.\d+)?)\s*hours?\s*per\s*week",
    ]
    for pattern in hour_patterns:
        match = re.search(pattern, compact, flags=re.I)
        if match:
            hours = float(match.group(1))
            break

    days = [day for day in DAY_NAMES if re.search(rf"\b{day[:3]}\w*\b", compact, re.I)]

    start_time = end_time = None
    time_match = re.search(
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s*[-–]\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))",
        compact,
        re.I,
    )
    if time_match:
        start_time = _normalise_time(time_match.group(1))
        end_time = _normalise_time(time_match.group(2))

    return Shift(
        raw_text=compact,
        hours_per_week=hours,
        days=days,
        start_time=start_time,
        end_time=end_time,
    )


def matches_rules(shift: Shift, rules: dict) -> tuple[bool, list[str]]:
    reasons = []

    max_hours = rules.get("max_hours_per_week")
    if max_hours is not None:
        if shift.hours_per_week is None:
            reasons.append("weekly hours could not be read")
        elif shift.hours_per_week > float(max_hours):
            reasons.append(
                f"{shift.hours_per_week:g}h exceeds maximum {float(max_hours):g}h"
            )

    blocked = set(rules.get("blocked_days", []))
    bad_days = blocked.intersection(shift.days)
    if bad_days:
        reasons.append("contains blocked day(s): " + ", ".join(sorted(bad_days)))

    allowed = set(rules.get("allowed_days", []))
    if allowed and shift.days and not set(shift.days).issubset(allowed):
        reasons.append("contains day(s) outside allowed days")

    if not rules.get("allow_night_shift", True):
        text = shift.raw_text.lower()
        if "night" in text or "overnight" in text:
            reasons.append("night shift is not allowed")

    earliest = rules.get("earliest_start_time")
    if earliest and shift.start_time and shift.start_time < earliest:
        reasons.append(f"starts before {earliest}")

    latest = rules.get("latest_end_time")
    if latest and shift.end_time and shift.end_time > latest:
        reasons.append(f"ends after {latest}")

    return (not reasons, reasons)


def score_shift(shift: Shift, rules: dict) -> int:
    score = 0
    preferred = rules.get("preferred_hours_order", [])
    if shift.hours_per_week is not None and shift.hours_per_week in preferred:
        score += (len(preferred) - preferred.index(shift.hours_per_week)) * 100

    score += 10 if shift.hours_per_week is not None else 0
    score += 5 if shift.start_time and shift.end_time else 0
    score += len(shift.days)
    return score
