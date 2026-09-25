from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amazon_shift_catcher.release_intelligence import (
    LondonFilter,
    ReleaseHistory,
    TelegramHistoryCollector,
    parse_telegram_export,
    summarise_releases,
)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def display_time(value: str) -> str:
    return value.replace("T", " ")[:16]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect and analyse London Amazon warehouse release alerts "
            "from the public Telegram history."
        )
    )
    parser.add_argument("--start", type=parse_date, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=parse_date, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-back days when --start is omitted",
    )
    parser.add_argument("--max-pages", type=int, default=120)
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Analyse the saved history without contacting Telegram",
    )
    parser.add_argument(
        "--data",
        default="data/london_releases.json",
        help="Path to the local JSON history",
    )
    parser.add_argument(
        "--telegram-export",
        help=(
            "Path to a Telegram Desktop machine-readable JSON export. "
            "When supplied, no Telegram web request is made."
        ),
    )
    args = parser.parse_args()

    end_date = args.end or date.today()
    start_date = args.start or (
        end_date - timedelta(days=max(1, args.days) - 1)
    )

    history = ReleaseHistory(args.data)

    if args.telegram_export:
        print(
            f"Reading Telegram export {args.telegram_export} "
            f"for {start_date} to {end_date}..."
        )
        releases = parse_telegram_export(
            args.telegram_export,
            start_date=start_date,
            end_date=end_date,
        )
        london = LondonFilter().filter(releases)
        saved = history.merge(london)
        print(
            f"Parsed {len(releases)} Amazon alert posts from the export; "
            f"{len(london)} classified as Greater London. "
            f"Saved history contains {len(saved)} London events."
        )
    elif not args.no_fetch:
        print(f"Collecting Telegram history from {start_date} to {end_date}...")
        collector = TelegramHistoryCollector()
        releases = collector.collect(
            start_date,
            end_date,
            max_pages=args.max_pages,
        )
        print(
            "Archive traversal: "
            f"{collector.pages_fetched} page(s), "
            f"stop={collector.stop_reason}, "
            f"oldest={collector.oldest_observed_date}, "
            f"newest={collector.newest_observed_date}"
        )
        london = LondonFilter().filter(releases)
        saved = history.merge(london)
        print(
            f"Collected {len(releases)} Amazon alert posts; "
            f"{len(london)} classified as Greater London. "
            f"Saved history contains {len(saved)} London events."
        )
    else:
        saved = history.load()

    summaries = summarise_releases(
        saved,
        start_date=start_date,
        end_date=end_date,
    )

    print()
    print(
        f"=== LONDON AMAZON RELEASE REPORT: "
        f"{start_date} to {end_date} ==="
    )
    print(
        "Telegram timestamps are delayed observations, "
        "not official Amazon first-seen times."
    )
    print()

    if not summaries:
        print("No London release records found for this period.")
        return 0

    header = (
        f"{'Job ID':<22} {'Area / postcode':<42} "
        f"{'Events':>6} {'Days':>5} {'Schedules':>9} "
        f"{'Last observed':<16} {'Hours'}"
    )
    print(header)
    print("-" * len(header))

    for item in summaries:
        area = ", ".join(item.locations)
        postcodes = ", ".join(item.postcodes)
        label = f"{area} [{postcodes}]"
        if len(label) > 41:
            label = label[:38] + "..."

        hours = ",".join(
            str(int(value)) if float(value).is_integer() else str(value)
            for value in item.hours_seen
        )

        print(
            f"{item.job_id:<22} {label:<42} "
            f"{item.observed_release_events:>6} "
            f"{item.active_days:>5} "
            f"{item.unique_schedules:>9} "
            f"{display_time(item.last_observed_at):<16} "
            f"{hours}"
        )

    print()
    print(
        "Events are distinct Telegram message IDs. "
        "The same schedule can appear again later and is kept as a "
        "separate observed event."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
