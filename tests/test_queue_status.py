from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from web_backend import Job, JobManager, format_wait_range


class QueueStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = JobManager(Path(self.directory.name))
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.manager.pool.shutdown, wait=True)

    def add(self, job_id, owner="private-owner", username="private-profile", **values):
        job = Job(job_id, owner, username, **values)
        self.manager.jobs[job.id] = job
        return job

    def snapshot(self, job_id="mine", now=1000):
        with patch("web_backend.time.time", return_value=now):
            return self.manager.snapshot("me", job_id)

    def test_queue_reports_fifo_position_and_cold_start_range_without_private_data(self):
        self.add("active-private-id", state="running", started=900, created=890)
        self.add("waiting-private-id", created=950)
        self.add("mine", owner="me", username="target", created=970)
        status = self.snapshot()
        self.assertEqual((status["queue_position"], status["queue_total"], status["jobs_ahead"]), (3, 3, 2))
        self.assertEqual(status["running_jobs"], 1)
        self.assertEqual((status["wait_seconds_min"], status["wait_seconds_max"]), (500, 1700))
        self.assertEqual(status["wait_estimate_basis"], "initial")
        self.assertEqual(status["queue_elapsed"], 30)
        for secret in ("private-owner", "private-profile", "active-private-id", "waiting-private-id"):
            self.assertNotIn(secret, repr(status))
        with self.assertRaises(ValueError):
            self.manager.snapshot("me", "active-private-id")

    def test_idle_worker_reports_imminent_start(self):
        self.add("mine", owner="me", created=999)
        status = self.snapshot()
        self.assertEqual(status["queue_position"], 1)
        self.assertEqual(status["jobs_ahead"], 0)
        self.assertEqual((status["wait_seconds_min"], status["wait_seconds_max"]), (0, 10))

    def test_history_scales_by_post_count_and_subtracts_active_elapsed_time(self):
        self.add("completed", state="complete", started=100, finished=500, total=100)
        self.add("active", state="running", started=900, total=50)
        self.add("mine", owner="me", created=970)
        before = self.snapshot(now=1000)
        after = self.snapshot(now=1020)
        self.assertEqual(before["wait_estimate_basis"], "history")
        self.assertEqual(before["wait_estimate_samples"], 1)
        self.assertEqual((before["wait_seconds_min"], before["wait_seconds_max"]), (40, 220))
        self.assertLess(after["wait_seconds_max"], before["wait_seconds_max"])

    def test_resumed_and_failed_runs_do_not_distort_full_run_estimate(self):
        self.add("resumed", state="complete", started=100, finished=105, resumed=True)
        self.add("failed", state="failed", started=110, finished=111)
        self.add("active", state="running", started=900)
        self.add("mine", owner="me", created=970)
        self.assertEqual(self.snapshot()["wait_estimate_basis"], "initial")

    def test_cancelling_waiting_job_immediately_moves_later_job_forward(self):
        self.add("active", state="running", started=900)
        cancelled = self.add("cancelled", owner="other", created=950)
        self.add("mine", owner="me", created=970)
        with patch("web_backend.time.time", return_value=1000):
            self.manager.cancel("other", "cancelled")
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(self.snapshot()["queue_position"], 2)
        self.manager._process = Mock()
        self.manager._run(cancelled, {})
        self.manager._process.assert_not_called()
        self.assertEqual(cancelled.state, "cancelled")

    def test_finished_running_job_moves_queue_forward(self):
        active = self.add("active", state="running", started=900)
        self.add("mine", owner="me", created=970)
        self.assertEqual(self.snapshot()["queue_position"], 2)
        active.state, active.finished = "complete", 1000
        status = self.snapshot()
        self.assertEqual((status["queue_position"], status["queue_total"]), (1, 1))
        self.assertEqual(status["wait_seconds_max"], 10)

    def test_overdue_running_job_never_promises_immediate_start(self):
        self.add("active", state="running", started=50)
        self.add("mine", owner="me", created=970)
        status = self.snapshot()
        self.assertTrue(status["wait_estimate_overdue"])
        self.assertGreater(status["wait_seconds_max"], 60)
        self.assertGreaterEqual(status["wait_seconds_min"], 0)

    def test_resubmission_timestamp_determines_fifo_position(self):
        self.add("old-job", state="queued", created=10, queued_at=990)
        self.add("mine", owner="me", created=970)
        status = self.snapshot()
        self.assertEqual(status["queue_position"], 1)

    def test_wait_ranges_round_to_minutes_and_do_not_show_negative_time(self):
        self.assertEqual(format_wait_range(0, 10), "до 10 с")
        self.assertEqual(format_wait_range(500, 901), "8–16 хв")
        self.assertEqual(format_wait_range(0, 125), "до 3 хв")

    def test_streamlit_shows_position_estimate_and_updated_queue(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        self.add("active", state="running", started=900)
        self.add("before-me", created=950)
        self.add("mine", owner="me", username="target", created=970)
        backend = Mock()
        backend.snapshot.return_value = self.snapshot()
        st.cache_resource.clear()
        try:
            with patch("web_backend.JobManager", return_value=backend):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=15)
                app.session_state["owner_id"] = "me"
                app.session_state["active_job"] = "mine"
                app.run()
                self.assertEqual(len(app.exception), 0)
                metrics = {item.label: item.value for item in app.metric}
                self.assertEqual(metrics["Ваше місце в черзі"], "3 з 3")
                self.assertEqual(metrics["Приблизно до старту"], "8–29 хв")
                self.assertTrue(any("Початкова груба оцінка" in item.value for item in app.caption))
                self.manager.jobs["before-me"].state = "cancelled"
                backend.snapshot.return_value = self.snapshot()
                app.run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual({item.label: item.value for item in app.metric}["Ваше місце в черзі"], "2 з 2")
        finally:
            st.cache_resource.clear()


if __name__ == "__main__":
    unittest.main()
