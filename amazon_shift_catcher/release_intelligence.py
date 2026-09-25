from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Optional
from urllib.parse import quote, urljoin

import requests


DEFAULT_CHANNEL_URL = "https://t.me/s/amazonukjobsalert"

_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.IGNORECASE,
)
_JOB_ID_RE = re.compile(r"\bJOB-UK-\d+\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_LIVE_RE = re.compile(r"was live for\s+([^\)\n]+)", re.IGNORECASE)
_LIVE_COMPONENT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)

_ALWAYS_LONDON_AREAS = {"E", "EC", "N", "NW", "SE", "SW", "W", "WC"}
_OUTER_LONDON_CANDIDATE_AREAS = {
    "BR",
    "CR",
    "DA",
    "EN",
    "HA",
    "IG",
    "KT",
    "RM",
    "SM",
    "TW",
    "UB",
    "WD",
}
_LONDON_BOROUGHS = {
    "Barking and Dagenham",
    "Barnet",
    "Bexley",
    "Brent",
    "Bromley",
    "Camden",
    "City of London",
    "Croydon",
    "Ealing",
    "Enfield",
    "Greenwich",
    "Hackney",
    "Hammersmith and Fulham",
    "Haringey",
    "Harrow",
    "Havering",
    "Hillingdon",
    "Hounslow",
    "Islington",
    "Kensington and Chelsea",
    "Kingston upon Thames",
    "Lambeth",
    "Lewisham",
    "Merton",
    "Newham",
    "Redbridge",
    "Richmond upon Thames",
    "Southwark",
    "Sutton",
    "Tower Hamlets",
    "Waltham Forest",
    "Wandsworth",
    "Westminster",
}


def normalise_postcode(value: str) -> str:
    compact = re.sub(r"\s+", "", value.upper())
    return f"{compact[:-3]} {compact[-3:]}" if len(compact) > 3 else compact


def postcode_area(postcode: str) -> str:
    match = re.match(r"([A-Z]{1,2})", postcode.upper().replace(" ", ""))
    return match.group(1) if match else ""


def parse_live_seconds(text: str) -> Optional[int]:
    match = _LIVE_RE.search(text)
    if not match:
        return None

    total_seconds = 0.0
    matched_component = False
    for value_text, unit_text in _LIVE_COMPONENT_RE.findall(match.group(1)):
        matched_component = True
        value = float(value_text)
        unit = unit_text.lower()
        if unit.startswith(("hour", "hr")) or unit == "h":
            total_seconds += value * 3600
        elif unit.startswith("min") or unit == "m":
            total_seconds += value * 60
        else:
            total_seconds += value

    return int(round(total_seconds)) if matched_component else None


@dataclass(frozen=True)
class TelegramRelease:
    message_id: str
    observed_at: str
    location: str
    postcode: str
    job_id: str
    role: str = ""
    employment: str = ""
    pay_gbp_per_hour: Optional[float] = None
    first_day: str = ""
    schedule: str = ""
    hours_per_week: Optional[float] = None
    live_seconds: Optional[int] = None
    source_url: str = ""

    @property
    def observed_datetime(self) -> datetime:
        return datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))

    @property
    def schedule_fingerprint(self) -> str:
        values = (
            self.job_id,
            self.postcode,
            self.first_day,
            self.schedule,
            "" if self.hours_per_week is None else str(self.hours_per_week),
        )
        return "|".join(value.strip().lower() for value in values)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TelegramRelease":
        return cls(**data)


@dataclass
class _RawMessage:
    post_ref: str = ""
    observed_at: str = ""
    text: str = ""


class _TelegramHTMLParser(HTMLParser):
    """Small parser for Telegram's public channel preview HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[_RawMessage] = []
        self.older_href: Optional[str] = None

        self._div_depth = 0
        self._wrap_depth: Optional[int] = None
        self._text_depth: Optional[int] = None
        self._current = _RawMessage()
        self._text_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, Optional[str]]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
    ) -> None:
        attr = self._attrs(attrs)

        if tag == "link":
            rel_tokens = set(attr.get("rel", "").split())
            if "prev" in rel_tokens and attr.get("href"):
                self.older_href = attr["href"]
            return

        if tag == "div":
            self._div_depth += 1
            classes = set(attr.get("class", "").split())

            if "tgme_widget_message_wrap" in classes and self._wrap_depth is None:
                self._wrap_depth = self._div_depth
                self._current = _RawMessage()
                self._text_parts = []

            if self._wrap_depth is not None:
                if "tgme_widget_message" in classes:
                    self._current.post_ref = attr.get("data-post", "")
                if "tgme_widget_message_text" in classes:
                    self._text_depth = self._div_depth
            return

        if self._wrap_depth is not None and tag == "time":
            observed = attr.get("datetime", "")
            if observed:
                self._current.observed_at = observed

        if self._text_depth is not None and tag == "br":
            self._text_parts.append("\n")

        if tag == "a":
            classes = set(attr.get("class", "").split())
            if "tme_messages_more" in classes and attr.get("href"):
                self.older_href = attr["href"]

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return

        if self._text_depth == self._div_depth:
            self._text_depth = None

        if self._wrap_depth == self._div_depth:
            self._current.text = "".join(self._text_parts).strip()
            if self._current.post_ref and self._current.observed_at:
                self.messages.append(self._current)
            self._current = _RawMessage()
            self._text_parts = []
            self._wrap_depth = None
            self._text_depth = None

        self._div_depth = max(0, self._div_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._text_depth is not None:
            self._text_parts.append(data)


def _release_from_raw(raw: _RawMessage, page_url: str) -> Optional[TelegramRelease]:
    text = raw.text
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    location_line = next((line for line in lines if line.startswith("📍")), "")
    postcode_match = _POSTCODE_RE.search(location_line)
    job_match = _JOB_ID_RE.search(text)
    if not postcode_match or not job_match:
        return None

    postcode = normalise_postcode(postcode_match.group(1))
    location = location_line.lstrip("📍").strip()
    postcode_match_in_location = _POSTCODE_RE.search(location)
    if postcode_match_in_location:
        location = location[: postcode_match_in_location.start()].strip()

    role_line = next((line for line in lines if line.startswith("🏷")), "")
    employment_line = next((line for line in lines if line.startswith("💼")), "")
    pay_line = next((line for line in lines if line.startswith("💰")), "")
    first_day_line = next((line for line in lines if "First Day:" in line), "")
    schedule_line = next((line for line in lines if "Schedule:" in line), "")
    hours_line = next((line for line in lines if "Hours/Week:" in line), "")

    pay_match = _NUMBER_RE.search(pay_line.replace(",", ""))
    hours_match = _NUMBER_RE.search(hours_line)

    post_ref = raw.post_ref.strip()
    message_id = post_ref.rsplit("/", 1)[-1] if "/" in post_ref else post_ref
    source_url = f"https://t.me/{post_ref}" if post_ref else page_url

    return TelegramRelease(
        message_id=message_id,
        observed_at=raw.observed_at,
        location=location,
        postcode=postcode,
        job_id=job_match.group(0).upper(),
        role=role_line.lstrip("🏷️").strip(),
        employment=employment_line.lstrip("💼").strip(),
        pay_gbp_per_hour=float(pay_match.group(1)) if pay_match else None,
        first_day=(
            first_day_line.split("First Day:", 1)[1].strip()
            if "First Day:" in first_day_line
            else ""
        ),
        schedule=(
            schedule_line.split("Schedule:", 1)[1].strip()
            if "Schedule:" in schedule_line
            else ""
        ),
        hours_per_week=float(hours_match.group(1)) if hours_match else None,
        live_seconds=parse_live_seconds(text),
        source_url=source_url,
    )


def parse_telegram_page(
    html: str,
    page_url: str = DEFAULT_CHANNEL_URL,
) -> tuple[list[TelegramRelease], Optional[str]]:
    parser = _TelegramHTMLParser()
    parser.feed(html)

    releases = []
    for raw in parser.messages:
        release = _release_from_raw(raw, page_url)
        if release is not None:
            releases.append(release)

    older_url = (
        urljoin(page_url, parser.older_href)
        if parser.older_href
        else None
    )
    return releases, older_url


def _flatten_telegram_export_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text_value = value.get("text", "")
        return text_value if isinstance(text_value, str) else str(text_value)
    if isinstance(value, list):
        return "".join(_flatten_telegram_export_text(item) for item in value)
    return ""


def parse_telegram_export(
    path: str | Path,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    channel_username: str = "amazonukjobsalert",
) -> list[TelegramRelease]:
    """Parse a Telegram Desktop machine-readable JSON channel export."""
    export_path = Path(path)
    raw = json.loads(export_path.read_text(encoding="utf-8"))
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Telegram export does not contain a messages list")

    releases: dict[str, TelegramRelease] = {}
    page_url = f"https://t.me/{channel_username}"

    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "message":
            continue

        message_id = str(message.get("id", "")).strip()
        observed_at = str(message.get("date", "")).strip()
        if not message_id or not observed_at:
            continue

        try:
            observed_date = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            ).date()
        except ValueError:
            continue

        if start_date and observed_date < start_date:
            continue
        if end_date and observed_date > end_date:
            continue

        text = _flatten_telegram_export_text(message.get("text", ""))
        if not text:
            continue

        raw_message = _RawMessage(
            post_ref=f"{channel_username}/{message_id}",
            observed_at=observed_at,
            text=text,
        )
        release = _release_from_raw(raw_message, page_url)
        if release is not None:
            releases[release.message_id] = release

    return sorted(
        releases.values(),
        key=lambda item: item.observed_datetime,
    )


def _fallback_older_url(
    channel_url: str,
    releases: Iterable[TelegramRelease],
) -> Optional[str]:
    """Build Telegram's older-page URL when the preview omits its Older link."""
    numeric_ids = [
        int(release.message_id)
        for release in releases
        if release.message_id.isdigit()
    ]
    if not numeric_ids:
        return None

    base_url = channel_url.split("?", 1)[0]
    return f"{base_url}?before={min(numeric_ids)}"


class TelegramHistoryCollector:
    """Collect public Telegram history at a deliberately low request rate."""

    def __init__(
        self,
        channel_url: str = DEFAULT_CHANNEL_URL,
        *,
        timeout_seconds: float = 20.0,
        pause_seconds: float = 1.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.channel_url = channel_url
        self.timeout_seconds = timeout_seconds
        self.pause_seconds = max(0.5, pause_seconds)
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "amazon-shift-catcher/1.0 historical-release-research",
        )
        self.pages_fetched = 0
        self.stop_reason = "not_started"
        self.oldest_observed_date: Optional[date] = None
        self.newest_observed_date: Optional[date] = None

    def fetch_page(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout_seconds)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "not provided")
            raise RuntimeError(
                "Telegram rate-limited the history request "
                f"(HTTP 429; Retry-After={retry_after}). Try again later."
            )
        response.raise_for_status()
        return response.text

    def collect(
        self,
        start_date: date,
        end_date: date,
        *,
        max_pages: int = 120,
    ) -> list[TelegramRelease]:
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")

        url: Optional[str] = self.channel_url
        seen_urls: set[str] = set()
        seen_page_signatures: set[tuple[str, ...]] = set()
        collected: dict[str, TelegramRelease] = {}

        self.pages_fetched = 0
        self.stop_reason = "running"
        self.oldest_observed_date = None
        self.newest_observed_date = None

        for page_index in range(max_pages):
            if not url:
                self.stop_reason = "no_older_page"
                break
            if url in seen_urls:
                self.stop_reason = "repeated_page_url"
                break
            seen_urls.add(url)

            html = self.fetch_page(url)
            self.pages_fetched += 1
            releases, older_url = parse_telegram_page(html, url)

            if not releases:
                self.stop_reason = "no_parseable_release_posts"
                break

            signature = tuple(sorted(release.message_id for release in releases))
            if signature in seen_page_signatures:
                self.stop_reason = "repeated_page_content"
                break
            seen_page_signatures.add(signature)

            if not older_url:
                older_url = _fallback_older_url(self.channel_url, releases)

            page_dates: list[date] = []
            for release in releases:
                observed_date = release.observed_datetime.date()
                page_dates.append(observed_date)

                if (
                    self.oldest_observed_date is None
                    or observed_date < self.oldest_observed_date
                ):
                    self.oldest_observed_date = observed_date
                if (
                    self.newest_observed_date is None
                    or observed_date > self.newest_observed_date
                ):
                    self.newest_observed_date = observed_date

                if start_date <= observed_date <= end_date:
                    collected[release.message_id] = release

            # Stop only when the entire parsed page is already older than
            # the requested range. A single pinned/out-of-order old post must
            # not terminate the backfill.
            if page_dates and max(page_dates) < start_date:
                self.stop_reason = "reached_before_start_date"
                break

            url = older_url
            if url and page_index + 1 < max_pages:
                time.sleep(self.pause_seconds)
        else:
            self.stop_reason = "max_pages_reached"

        return sorted(
            collected.values(),
            key=lambda item: item.observed_datetime,
        )


class LondonFilter:
    """
    Filter Greater London releases.

    Central London postcode areas are classified locally. Ambiguous outer
    postcode areas are checked with postcodes.io and cached locally.
    """

    def __init__(
        self,
        cache_path: str | Path = "data/postcode_london_cache.json",
        *,
        timeout_seconds: float = 8.0,
        lookup: Optional[Callable[[str], Optional[bool]]] = None,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.timeout_seconds = timeout_seconds
        self.lookup = lookup
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, bool]:
        if not self.cache_path.exists():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return {str(key): bool(value) for key, value in raw.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _api_lookup(self, postcode: str) -> Optional[bool]:
        try:
            response = requests.get(
                f"https://api.postcodes.io/postcodes/{quote(postcode)}",
                timeout=self.timeout_seconds,
                headers={"User-Agent": "amazon-shift-catcher/1.0 london-filter"},
            )
        except requests.RequestException:
            return None

        if response.status_code == 404:
            return False
        if response.status_code == 429:
            return None

        try:
            response.raise_for_status()
            result = response.json().get("result") or {}
        except (requests.RequestException, ValueError, AttributeError):
            return None

        region = str(result.get("region") or "")
        admin_district = str(result.get("admin_district") or "")
        return region == "London" or admin_district in _LONDON_BOROUGHS

    def is_london(self, postcode: str, location: str = "") -> bool:
        area = postcode_area(postcode)

        if area in _ALWAYS_LONDON_AREAS:
            return True

        if area not in _OUTER_LONDON_CANDIDATE_AREAS:
            return False

        key = re.sub(r"\s+", "", postcode.upper())
        if key in self._cache:
            return self._cache[key]

        if "london" in location.lower():
            self._cache[key] = True
            self._save_cache()
            return True

        result = self.lookup(postcode) if self.lookup else self._api_lookup(postcode)
        if result is not None:
            self._cache[key] = bool(result)
            self._save_cache()
            return bool(result)

        # Conservative fallback for ambiguous outer-London postcode areas.
        return False

    def filter(
        self,
        releases: Iterable[TelegramRelease],
    ) -> list[TelegramRelease]:
        return [
            release
            for release in releases
            if self.is_london(release.postcode, release.location)
        ]


class ReleaseHistory:
    """JSON-backed release history keyed by Telegram message id."""

    def __init__(
        self,
        path: str | Path = "data/london_releases.json",
    ) -> None:
        self.path = Path(path)

    def load(self) -> list[TelegramRelease]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [TelegramRelease.from_dict(item) for item in raw]

    def merge(
        self,
        releases: Iterable[TelegramRelease],
    ) -> list[TelegramRelease]:
        merged = {item.message_id: item for item in self.load()}
        for release in releases:
            merged[release.message_id] = release

        result = sorted(
            merged.values(),
            key=lambda item: item.observed_datetime,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [item.to_dict() for item in result],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return result


@dataclass(frozen=True)
class WarehouseSummary:
    job_id: str
    locations: tuple[str, ...]
    postcodes: tuple[str, ...]
    observed_release_events: int
    active_days: int
    unique_schedules: int
    first_observed_at: str
    last_observed_at: str
    hours_seen: tuple[float, ...]
    median_live_seconds: Optional[float]


def summarise_releases(
    releases: Iterable[TelegramRelease],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[WarehouseSummary]:
    grouped: dict[str, list[TelegramRelease]] = defaultdict(list)

    for release in releases:
        observed_date = release.observed_datetime.date()
        if start_date and observed_date < start_date:
            continue
        if end_date and observed_date > end_date:
            continue
        grouped[release.job_id].append(release)

    summaries: list[WarehouseSummary] = []
    for job_id, items in grouped.items():
        ordered = sorted(items, key=lambda item: item.observed_datetime)
        live_values = [
            item.live_seconds
            for item in ordered
            if item.live_seconds is not None
        ]
        hours = sorted(
            {
                item.hours_per_week
                for item in ordered
                if item.hours_per_week is not None
            }
        )

        summaries.append(
            WarehouseSummary(
                job_id=job_id,
                locations=tuple(sorted({item.location for item in ordered})),
                postcodes=tuple(sorted({item.postcode for item in ordered})),
                observed_release_events=len(
                    {item.message_id for item in ordered}
                ),
                active_days=len(
                    {item.observed_datetime.date() for item in ordered}
                ),
                unique_schedules=len(
                    {item.schedule_fingerprint for item in ordered}
                ),
                first_observed_at=ordered[0].observed_at,
                last_observed_at=ordered[-1].observed_at,
                hours_seen=tuple(hours),
                median_live_seconds=(
                    float(median(live_values))
                    if live_values
                    else None
                ),
            )
        )

    summaries.sort(
        key=lambda item: (
            -item.observed_release_events,
            -item.active_days,
            -item.unique_schedules,
            -datetime.fromisoformat(
                item.last_observed_at.replace("Z", "+00:00")
            ).timestamp(),
        )
    )
    return summaries
