from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amazon_shift_catcher.release_intelligence import (
    LondonFilter,
    TelegramRelease,
    cluster_release_sessions,
    parse_telegram_export,
    summarise_releases,
)


DEFAULT_EXPORT = ROOT / "data" / "telegram" / "result.json"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\b")


def _schedule_days(schedule: str) -> set[str]:
    return {
        day
        for day in WEEKDAYS
        if re.search(rf"\b{re.escape(day)}\b", schedule or "", re.IGNORECASE)
    }


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path


@st.cache_data(show_spinner=False)
def _load_releases(
    export_path: str,
    modified_ns: int,
    london_only: bool,
) -> list[TelegramRelease]:
    del modified_ns
    releases = parse_telegram_export(export_path)
    if london_only:
        return LondonFilter().filter(releases)
    return releases


def _nation_from_location(location: str) -> str:
    value = (location or "").lower()
    if "northern ireland" in value:
        return "Northern Ireland"
    if "scotland" in value:
        return "Scotland"
    if "wales" in value:
        return "Wales"
    if "england" in value:
        return "England"
    return "Unknown"


def _release_options(release: TelegramRelease) -> list[dict]:
    first_days = release.first_day_options or (
        (release.first_day,) if release.first_day else ()
    )
    schedules = release.schedule_options or (
        (release.schedule,) if release.schedule else ()
    )
    hours = release.all_hours_per_week

    option_count = max(len(first_days), len(schedules), len(hours), 1)
    rows: list[dict] = []

    for index in range(option_count):
        first_day = first_days[index] if index < len(first_days) else ""
        schedule = schedules[index] if index < len(schedules) else ""
        weekly_hours = hours[index] if index < len(hours) else None
        match = _TIME_RE.search(schedule)
        start_hour = int(match.group(1)) if match else None

        rows.append(
            {
                "message_id": release.message_id,
                "observed_at": release.observed_datetime,
                "job_id": release.job_id,
                "location": release.location,
                "nation": _nation_from_location(release.location),
                "postcode": release.postcode,
                "role": release.role,
                "employment": release.employment,
                "pay_gbp_per_hour": release.pay_gbp_per_hour,
                "first_day": first_day,
                "schedule": schedule,
                "weekly_hours": weekly_hours,
                "start_hour": start_hour,
                "live_seconds": release.live_seconds,
            }
        )

    return rows


def _options_dataframe(releases: list[TelegramRelease]) -> pd.DataFrame:
    rows = [
        row
        for release in releases
        for row in _release_options(release)
    ]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    frame["observed_at"] = pd.to_datetime(frame["observed_at"])
    frame["observed_date"] = frame["observed_at"].dt.date
    return frame


def _rebuild_filtered_releases(
    original_releases: list[TelegramRelease],
    filtered_options: pd.DataFrame,
) -> list[TelegramRelease]:
    if filtered_options.empty:
        return []

    by_message = {
        release.message_id: release for release in original_releases
    }
    rebuilt: list[TelegramRelease] = []

    for message_id, group in filtered_options.groupby("message_id", sort=False):
        release = by_message.get(str(message_id))
        if release is None:
            continue

        first_days = tuple(
            str(value)
            for value in group["first_day"].tolist()
            if pd.notna(value) and str(value)
        )
        schedules = tuple(
            str(value)
            for value in group["schedule"].tolist()
            if pd.notna(value) and str(value)
        )
        hours = tuple(
            float(value)
            for value in group["weekly_hours"].tolist()
            if pd.notna(value)
        )

        rebuilt.append(
            replace(
                release,
                first_day=first_days[0] if first_days else "",
                schedule=schedules[0] if schedules else "",
                hours_per_week=hours[0] if hours else None,
                first_day_options=first_days,
                schedule_options=schedules,
                hours_per_week_options=hours,
            )
        )

    return sorted(rebuilt, key=lambda item: item.observed_datetime)


def _format_hours(values: tuple[float, ...]) -> str:
    parts = []
    for value in values:
        parts.append(str(int(value)) if float(value).is_integer() else str(value))
    return ", ".join(parts)


def _summary_dataframe(
    releases: list[TelegramRelease],
    session_gap_minutes: float,
) -> pd.DataFrame:
    summaries = summarise_releases(
        releases,
        session_gap_minutes=session_gap_minutes,
    )
    rows = []
    for item in summaries:
        session_count = item.probable_release_sessions
        twenty_share = (
            100.0 * item.sessions_with_20_hours / session_count
            if session_count
            else 0.0
        )
        rows.append(
            {
                "Job ID": item.job_id,
                "Area / postcode": (
                    f"{', '.join(item.locations)} "
                    f"[{', '.join(item.postcodes)}]"
                ),
                "Messages": item.observed_release_events,
                "Probable sessions": session_count,
                "20h sessions": item.sessions_with_20_hours,
                "20h share %": round(twenty_share, 1),
                "Active days": item.active_days,
                "Schedule variants": item.unique_schedules,
                "Peak observed hours": ", ".join(
                    f"{hour:02d}:00" for hour in item.common_session_hours
                ),
                "Last observed": item.last_observed_at.replace("T", " ")[:16],
                "Hours seen": _format_hours(item.hours_seen),
                "Median msgs/session": round(item.median_messages_per_session, 1),
                "Median live sec": item.median_live_seconds,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    st.set_page_config(
        page_title="Amazon UK Shift Intelligence",
        page_icon="📦",
        layout="wide",
    )
    st.title("Amazon UK Shift Intelligence")
    st.caption(
        "Filter historical Amazon shift alerts across the UK from your local "
        "Telegram export. Telegram timestamps are delayed observations, not "
        "official Amazon release times."
    )

    with st.sidebar:
        st.header("Data")
        export_value = st.text_input(
            "Telegram result.json",
            value=str(DEFAULT_EXPORT.relative_to(ROOT)),
        )
        coverage = st.selectbox(
            "UK coverage",
            options=("Greater London", "All UK"),
            index=0,
            help=(
                "Keep Greater London for your current search, or switch to "
                "All UK whenever you want to analyse other locations."
            ),
        )

    export_path = _resolve_path(export_value)
    if not export_path.exists():
        st.error(
            f"Telegram export not found: {export_path}. "
            "Put result.json in data/telegram/ or change the path in the sidebar."
        )
        st.stop()

    london_only = coverage == "Greater London"
    spinner_text = (
        "Reading Telegram export and filtering Greater London..."
        if london_only
        else "Reading Telegram export for all UK locations..."
    )
    with st.spinner(spinner_text):
        scoped_releases = _load_releases(
            str(export_path),
            export_path.stat().st_mtime_ns,
            london_only,
        )

    options = _options_dataframe(scoped_releases)
    if options.empty:
        st.warning(f"No Amazon shift records were found for {coverage}.")
        st.stop()

    min_date = options["observed_date"].min()
    max_date = options["observed_date"].max()
    all_job_ids = sorted(options["job_id"].dropna().unique().tolist())
    all_nations = sorted(
        value
        for value in options["nation"].dropna().unique().tolist()
        if value != "Unknown"
    )
    all_locations = sorted(
        value for value in options["location"].dropna().unique().tolist() if value
    )
    all_hours = sorted(
        float(value)
        for value in options["weekly_hours"].dropna().unique().tolist()
    )

    with st.sidebar:
        st.header("Filters")
        selected_dates = st.date_input(
            "Observed date range",
            value=(min_date, max_date),
            help=(
                "Choose any start and end dates. Dates outside the current "
                "Telegram export are allowed but will simply return no records."
            ),
        )
        if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
            start_date, end_date = selected_dates
        elif isinstance(selected_dates, list) and len(selected_dates) == 2:
            start_date, end_date = selected_dates
        else:
            start_date = end_date = selected_dates

        selected_nations = st.multiselect(
            "Nation",
            options=all_nations,
            placeholder="All nations",
            help="Useful when UK coverage is set to All UK.",
        )
        selected_locations = st.multiselect(
            "Area / location",
            options=all_locations,
            placeholder="All areas",
            help=(
                "Choose one or more exact areas from the Telegram data. "
                "Leave empty to include every area in the selected UK coverage."
            ),
        )
        selected_jobs = st.multiselect(
            "Job ID",
            options=all_job_ids,
            placeholder="All job IDs",
        )
        location_search = st.text_input(
            "Area / postcode contains",
            placeholder="e.g. Croydon, Enfield, Manchester, NW10",
        )
        selected_hours = st.multiselect(
            "Weekly hours",
            options=all_hours,
            placeholder="All weekly hours",
            format_func=lambda value: (
                str(int(value)) if float(value).is_integer() else str(value)
            ),
        )
        only_20h = st.checkbox("Only 20-hour options", value=False)
        selected_days = st.multiselect(
            "Allowed working days",
            options=list(WEEKDAYS),
            placeholder="Any day",
            help=(
                "When days are selected, a shift is shown only if every "
                "working day in its schedule is inside your selected days. "
                "For example, selecting Wed-Sun excludes any shift containing "
                "Mon or Tue."
            ),
        )
        start_hour_range = st.slider(
            "Shift start hour",
            min_value=0,
            max_value=23,
            value=(0, 23),
            help="Uses the start time written in the Telegram schedule.",
        )
        session_gap = st.slider(
            "Probable session gap (minutes)",
            min_value=1,
            max_value=60,
            value=15,
            help=(
                "Repeated alerts for the same Job ID are grouped while each "
                "consecutive gap stays within this many minutes."
            ),
        )

    filtered = options[
        (options["observed_date"] >= start_date)
        & (options["observed_date"] <= end_date)
    ].copy()

    if selected_nations:
        filtered = filtered[filtered["nation"].isin(selected_nations)]

    if selected_locations:
        filtered = filtered[filtered["location"].isin(selected_locations)]

    if selected_jobs:
        filtered = filtered[filtered["job_id"].isin(selected_jobs)]

    if location_search.strip():
        query = location_search.strip()
        location_blob = (
            filtered["location"].fillna("")
            + " "
            + filtered["postcode"].fillna("")
            + " "
            + filtered["job_id"].fillna("")
        )
        filtered = filtered[
            location_blob.str.contains(query, case=False, regex=False)
        ]

    hour_filter = {20.0} if only_20h else {float(v) for v in selected_hours}
    if hour_filter:
        filtered = filtered[filtered["weekly_hours"].isin(hour_filter)]

    if selected_days:
        allowed_days = set(selected_days)
        filtered = filtered[
            filtered["schedule"].fillna("").map(
                lambda schedule: (
                    bool(_schedule_days(schedule))
                    and _schedule_days(schedule).issubset(allowed_days)
                )
            )
        ]

    if start_hour_range != (0, 23):
        low_hour, high_hour = start_hour_range
        filtered = filtered[
            filtered["start_hour"].notna()
            & filtered["start_hour"].between(low_hour, high_hour)
        ]

    filtered_releases = _rebuild_filtered_releases(
        scoped_releases,
        filtered,
    )
    sessions = cluster_release_sessions(
        filtered_releases,
        gap_minutes=float(session_gap),
    )
    summary = _summary_dataframe(
        filtered_releases,
        session_gap_minutes=float(session_gap),
    )

    twenty_sessions = sum(session.has_20_hour_option for session in sessions)
    metric_columns = st.columns(5)
    metric_columns[0].metric("Shift messages", len(filtered_releases))
    metric_columns[1].metric("Probable sessions", len(sessions))
    metric_columns[2].metric("20h sessions", twenty_sessions)
    metric_columns[3].metric(
        "Active job IDs",
        len({release.job_id for release in filtered_releases}),
    )
    metric_columns[4].metric(
        "Schedule options",
        len(filtered),
    )

    if filtered.empty:
        st.info("No records match the current filters.")
        st.stop()

    st.subheader("Warehouse summary")
    st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
        column_config={
            "20h share %": st.column_config.NumberColumn(format="%.1f%%"),
            "Median live sec": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    chart_left, chart_right = st.columns(2)

    with chart_left:
        st.subheader("Probable sessions by job")
        if not summary.empty:
            chart_data = (
                summary.set_index("Job ID")[
                    ["Probable sessions", "20h sessions"]
                ]
                .head(15)
            )
            st.bar_chart(chart_data)

    with chart_right:
        st.subheader("Sessions by observed day")
        if sessions:
            daily = pd.DataFrame(
                {
                    "date": [
                        session.started_datetime.date()
                        for session in sessions
                    ]
                }
            )
            daily = (
                daily.value_counts("date")
                .sort_index()
                .rename("Probable sessions")
                .to_frame()
            )
            st.line_chart(daily)

    st.subheader("Filtered shift options")
    display_columns = [
        "observed_at",
        "job_id",
        "location",
        "postcode",
        "weekly_hours",
        "first_day",
        "schedule",
        "pay_gbp_per_hour",
        "live_seconds",
        "message_id",
    ]
    display = filtered[display_columns].sort_values(
        "observed_at",
        ascending=False,
    )
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.download_button(
        "Download filtered CSV",
        data=display.to_csv(index=False).encode("utf-8"),
        file_name=(
            f"amazon-uk-shifts-{start_date}-to-{end_date}.csv"
        ),
        mime="text/csv",
    )

    with st.expander("How to read the dashboard"):
        st.write(
            "Probable sessions are inferred from Telegram observations: "
            "consecutive alerts for the same Amazon Job ID are grouped using "
            "the session-gap setting. They are not confirmed Amazon release "
            "events. Peak/observed times can be delayed relative to the "
            "official Amazon site."
        )


if __name__ == "__main__":
    main()
