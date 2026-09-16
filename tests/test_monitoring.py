import tempfile
import unittest
from pathlib import Path

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
            self.assertIn('"shift_batches_seen": 1', text)
            self.assertIn('"screenshots_saved": 2', text)


if __name__ == "__main__":
    unittest.main()
