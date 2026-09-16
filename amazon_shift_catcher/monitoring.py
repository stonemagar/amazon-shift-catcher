import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class MonitorState(str, Enum):
    STARTING = "STARTING"
    MONITORING = "MONITORING"
    SHIFT_FOUND = "SHIFT_FOUND"
    PAUSED_403 = "PAUSED_403"
    RECOVERING = "RECOVERING"
    SECURITY_CHECK = "SECURITY_CHECK"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


def rank_hours(hours: list[float], preferred: float) -> list[float]:
    """Put exact preferred hours first while preserving all other page order."""
    return sorted(hours, key=lambda value: 0 if value == preferred else 1)


@dataclass
class SessionMetrics:
    session_id: str = field(
        default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    started_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    state: str = MonitorState.STARTING.value
    state_history: list[dict] = field(default_factory=list)

    checks_total: int = 0
    _last_check_monotonic: Optional[float] = field(default=None, repr=False)
    _check_interval_total: float = field(default=0.0, repr=False)
    _check_interval_samples: int = field(default=0, repr=False)
    check_interval_max_seconds: float = 0.0

    # Normal monitoring timing is tracked separately so deliberate 403 pauses do
    # not inflate the real shift-check interval.
    _normal_last_check_monotonic: Optional[float] = field(default=None, repr=False)
    _normal_check_interval_total: float = field(default=0.0, repr=False)
    _normal_check_interval_samples: int = field(default=0, repr=False)
    normal_check_interval_max_seconds: float = 0.0

    reload_cycles: int = 0
    reload_total_seconds: float = 0.0
    reload_max_seconds: float = 0.0

    blocked_events: int = 0
    blocked_total_seconds: float = 0.0
    blocked_max_seconds: float = 0.0
    blocked_durations: list[float] = field(default_factory=list)
    recovery_tests: int = 0
    manual_recovery_count: int = 0
    automatic_recovery_count: int = 0

    shift_batches_seen: int = 0
    priority_shift_batches_seen: int = 0
    visible_shifts_seen: int = 0

    screenshots_saved: int = 0
    notifications_sent: int = 0
    errors: int = 0
    last_error: Optional[str] = None

    ended_at: Optional[str] = None
    stop_reason: Optional[str] = None

    def transition(self, new_state: MonitorState, log, reason: str | None = None) -> None:
        old_state = self.state
        self.state = new_state.value
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "from": old_state,
            "to": new_state.value,
        }
        if reason:
            entry["reason"] = reason
        self.state_history.append(entry)

        # Break the normal-check timing chain when a 403 pause begins and again
        # when normal monitoring resumes. This prevents the blocked period from
        # being counted as a normal monitoring interval.
        if new_state == MonitorState.PAUSED_403:
            self._normal_last_check_monotonic = None

        if (
            new_state == MonitorState.MONITORING
            and old_state in {MonitorState.PAUSED_403.value, MonitorState.RECOVERING.value}
        ):
            self._normal_last_check_monotonic = None
            reason_lower = (reason or "").lower()
            if "automatic recovery reload" in reason_lower:
                self.automatic_recovery_count += 1
            elif "manual" in reason_lower or "current page" in reason_lower:
                self.manual_recovery_count += 1

        if old_state != new_state.value:
            if reason:
                log.info("STATE %s -> %s | %s", old_state, new_state.value, reason)
            else:
                log.info("STATE %s -> %s", old_state, new_state.value)

    def mark_check(self) -> None:
        now = time.monotonic()
        self.checks_total += 1

        # Legacy/all-state interval metric. Kept for backward compatibility.
        if self._last_check_monotonic is not None:
            interval = now - self._last_check_monotonic
            self._check_interval_total += interval
            self._check_interval_samples += 1
            self.check_interval_max_seconds = max(
                self.check_interval_max_seconds,
                interval,
            )
        self._last_check_monotonic = now

        # Real normal monitoring interval: only samples while MONITORING and the
        # baseline is reset across 403 recovery periods.
        if self.state == MonitorState.MONITORING.value:
            if self._normal_last_check_monotonic is not None:
                normal_interval = now - self._normal_last_check_monotonic
                self._normal_check_interval_total += normal_interval
                self._normal_check_interval_samples += 1
                self.normal_check_interval_max_seconds = max(
                    self.normal_check_interval_max_seconds,
                    normal_interval,
                )
            self._normal_last_check_monotonic = now

    def record_reload(self, seconds: float) -> None:
        self.reload_cycles += 1
        self.reload_total_seconds += max(0.0, seconds)
        self.reload_max_seconds = max(self.reload_max_seconds, seconds)

    def record_403(self, seconds: float, tests: int) -> None:
        observed = max(0.0, seconds)
        self.blocked_events += 1
        self.blocked_total_seconds += observed
        self.blocked_max_seconds = max(self.blocked_max_seconds, observed)
        self.blocked_durations.append(observed)
        self.recovery_tests += max(0, tests)

    def record_shift_batch(self, visible_count: int, priority_count: int) -> None:
        self.shift_batches_seen += 1
        self.visible_shifts_seen += max(0, visible_count)
        if priority_count > 0:
            self.priority_shift_batches_seen += 1

    def record_screenshot(self, count: int = 1) -> None:
        self.screenshots_saved += max(0, count)

    def record_notification(self, count: int = 1) -> None:
        self.notifications_sent += max(0, count)

    def record_error(self, error: Exception | str) -> None:
        self.errors += 1
        self.last_error = str(error)

    def finish(self, reason: str) -> None:
        self.ended_at = datetime.now().isoformat(timespec="seconds")
        self.stop_reason = reason

    def to_dict(self) -> dict:
        avg_check = (
            self._check_interval_total / self._check_interval_samples
            if self._check_interval_samples
            else 0.0
        )
        normal_avg_check = (
            self._normal_check_interval_total / self._normal_check_interval_samples
            if self._normal_check_interval_samples
            else 0.0
        )
        avg_reload = (
            self.reload_total_seconds / self.reload_cycles
            if self.reload_cycles
            else 0.0
        )
        avg_403 = (
            self.blocked_total_seconds / self.blocked_events
            if self.blocked_events
            else 0.0
        )

        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "stop_reason": self.stop_reason,
            "state": self.state,
            "state_history": self.state_history,
            "checks_total": self.checks_total,
            "average_check_interval_seconds": round(avg_check, 3),
            "max_check_interval_seconds": round(self.check_interval_max_seconds, 3),
            "normal_average_check_interval": round(normal_avg_check, 3),
            "normal_max_check_interval_seconds": round(
                self.normal_check_interval_max_seconds,
                3,
            ),
            "reload_cycles": self.reload_cycles,
            "average_reload_seconds": round(avg_reload, 3),
            "max_reload_seconds": round(self.reload_max_seconds, 3),
            "403_count": self.blocked_events,
            "403_average_duration_seconds": round(avg_403, 3),
            "403_max_duration_seconds": round(self.blocked_max_seconds, 3),
            "403_recovery_tests": self.recovery_tests,
            # Requested professional recovery metrics:
            # - 403_pause_time: cumulative seconds normal monitoring was paused.
            # - 403_observed_duration: average observed 403 duration per event.
            "403_pause_time": round(self.blocked_total_seconds, 3),
            "403_observed_duration": round(avg_403, 3),
            "403_observed_durations": [
                round(value, 3) for value in self.blocked_durations
            ],
            "manual_recovery_count": self.manual_recovery_count,
            "automatic_recovery_count": self.automatic_recovery_count,
            "shift_batches_seen": self.shift_batches_seen,
            "priority_shift_batches_seen": self.priority_shift_batches_seen,
            "visible_shifts_seen": self.visible_shifts_seen,
            "screenshots_saved": self.screenshots_saved,
            "notifications_sent": self.notifications_sent,
            "errors": self.errors,
            "last_error": self.last_error,
        }

    def save(self, log_dir: Path) -> tuple[Path, Path]:
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2)
        session_path = log_dir / f"session-metrics-{self.session_id}.json"
        latest_path = log_dir / "latest-session-metrics.json"
        session_path.write_text(payload + "\n", encoding="utf-8")
        latest_path.write_text(payload + "\n", encoding="utf-8")
        return session_path, latest_path
