import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amazon_shift_catcher.monitoring import MonitorState, SessionMetrics, rank_hours


class DummyLog:
    def info(self, *args, **kwargs):
        pass


class MonitoringTests(unittest.TestCase):
    def test_priority_hours_are_ranked_first_without_dropping_others(self):
        hours = [10.0, 30.0, 20.0, 40.0, 20.0]
        self.assertEqual(
            rank_hours(hours, 20.0),
            [20.0, 20.0, 10.0, 30.0, 40.0],
        )

    def test_state_transition_is_recorded(self):
        metrics = SessionMetrics()
        metrics.transition(MonitorState.MONITORING, DummyLog(), "ready")
        self.assertEqual(metrics.state, MonitorState.MONITORING.value)
        self.assertEqual(metrics.state_history[-1]["to"], "MONITORING")
        self.assertEqual(metrics.state_history[-1]["reason"], "ready")

    def test_normal_average_excludes_403_pause(self):
        metrics = SessionMetrics()
        log = DummyLog()
        metrics.transition(MonitorState.MONITORING, log, "ready")

        with patch("amazon_shift_catcher.monitoring.time.monotonic", side_effect=[0, 12, 200, 212]):
            metrics.mark_check()  # baseline at 0
            metrics.mark_check()  # normal interval = 12
            metrics.transition(MonitorState.PAUSED_403, log, "CloudFront 403 detected")
            metrics.transition(
                MonitorState.MONITORING,
                log,
                "403 cleared after 188 seconds (manual/current-page refresh detected)",
            )
            metrics.mark_check()  # new normal baseline at 200
            metrics.mark_check()  # normal interval = 12

        summary = metrics.to_dict()
        self.assertEqual(summary["normal_average_check_interval"], 12.0)
        self.assertEqual(summary["manual_recovery_count"], 1)
        self.assertEqual(summary["automatic_recovery_count"], 0)

    def test_403_metrics_count_manual_and_automatic_recovery(self):
        metrics = SessionMetrics()
        log = DummyLog()

        metrics.transition(MonitorState.PAUSED_403, log, "CloudFront 403 detected")
        metrics.record_403(157.25, 2)
        metrics.transition(
            MonitorState.MONITORING,
            log,
            "403 cleared after 157 seconds (manual/current-page refresh detected)",
        )

        metrics.transition(MonitorState.PAUSED_403, log, "CloudFront 403 detected")
        metrics.record_403(61.5, 1)
        metrics.transition(
            MonitorState.MONITORING,
            log,
            "403 cleared after 61 seconds (automatic recovery reload)",
        )

        summary = metrics.to_dict()
        self.assertEqual(summary["403_pause_time"], 218.75)
        self.assertEqual(summary["403_observed_duration"], 109.375)
        self.assertEqual(summary["403_observed_durations"], [157.25, 61.5])
        self.assertEqual(summary["manual_recovery_count"], 1)
        self.assertEqual(summary["automatic_recovery_count"], 1)
        self.assertEqual(summary["403_recovery_tests"], 3)

    def test_metrics_are_saved_as_json(self):
        metrics = SessionMetrics()
        metrics.record_reload(1.5)
        metrics.record_403(62.0, 1)
        metrics.record_shift_batch(3, 1)
        metrics.record_screenshot(2)
        metrics.record_notification()
        metrics.finish("test complete")

        with tempfile.TemporaryDirectory() as tmp:
            session_path, latest_path = metrics.save(Path(tmp))
            self.assertTrue(session_path.exists())
            self.assertTrue(latest_path.exists())
            text = latest_path.read_text(encoding="utf-8")
            self.assertIn('"403_count": 1', text)
            self.assertIn('"403_pause_time": 62.0', text)
            self.assertIn('"403_observed_duration": 62.0', text)
            self.assertIn('"manual_recovery_count": 0', text)
            self.assertIn('"automatic_recovery_count": 0', text)
            self.assertIn('"shift_batches_seen": 1', text)
            self.assertIn('"screenshots_saved": 2', text)


if __name__ == "__main__":
    unittest.main()
