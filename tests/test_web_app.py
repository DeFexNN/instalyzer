from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web_backend import Job, JobManager, normalize_profile, password_matches, result_cost


class WebBoundaryTests(unittest.TestCase):
    def test_profile_input_rejects_non_instagram_hosts_and_post_links(self):
        self.assertEqual(normalize_profile("https://www.instagram.com/example.user/"), "example.user")
        for value in ("https://example.com/user/", "https://instagram.com/p/123/", "../../.env"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_profile(value)

    def test_total_usage_does_not_add_synthesis_twice(self):
        report = {"model_usage": {"cost_usd": 0.02, "cost_is_complete": True}, "summary_usage": {"cost_usd": 0.003}}
        self.assertEqual(result_cost(report), 0.02)
        report["model_usage"]["cost_is_complete"] = False
        self.assertIsNone(result_cost(report))

    def test_access_code_is_required_and_compared_exactly(self):
        self.assertFalse(password_matches("", {}))
        self.assertFalse(password_matches("wrong", {"APP_PASSWORD": "a-strong-demo-password"}))
        self.assertTrue(password_matches("a-strong-demo-password", {"APP_PASSWORD": "a-strong-demo-password"}))

    def test_mock_pipeline_produces_private_downloadable_artifacts(self):
        class OfflineManager(JobManager):
            def _process(self, job, script, arguments, env, config):
                if script == "scrape_instagram.py":
                    assert env["IG_INCLUDE_COMMENTS"] == "true"
                    assert int(env["OPENCODE_POST_MAX_OUTPUT_TOKENS"]) >= 2048
                    assert int(env["OPENCODE_MAX_MEDIA_PER_POST"]) >= 6
                    export = Path(env["IG_OUTPUT_DIR"]) / "fixture"
                    (export / "media").mkdir(parents=True)
                    (export / "media" / "photo.jpg").write_bytes(b"test-image")
                    (export / "posts.json").write_text(json.dumps({"posts": [{"id": "1", "comments": [],
                        "comments_status": "available", "media_files": [{"file": "photo.jpg"}]}]}), encoding="utf-8")
                else:
                    export = Path(arguments[0])
                    (export / "profile_analysis.json").write_text(json.dumps({"account": job.username, "profile_analysis": "Test conclusion", "post_count": 1}), encoding="utf-8")
                    (export / "profile_analysis.md").write_text("# Test conclusion", encoding="utf-8")

        config = {"APP_PASSWORD": "a-strong-demo-password", "IG_USERNAME": "owner", "IG_SESSIONID": "local-test-session",
            "IG_CSRFTOKEN": "local-test-csrf", "IG_DS_USER_ID": "123", "OPENCODE_API_KEY": "local-test-api"}
        with tempfile.TemporaryDirectory() as temporary:
            jobs = OfflineManager(Path(temporary))
            try:
                with self.assertRaises(ValueError):
                    jobs.submit("owner-a", "example", config, authorized=False)
                job_id = jobs.submit("owner-a", "example", config, authorized=True)
                jobs.pool.shutdown(wait=True)
                self.assertEqual(jobs.snapshot("owner-a", job_id)["state"], "complete")
                self.assertEqual(jobs.result("owner-a", job_id)["account"], "example")
                self.assertIn("web_execution", jobs.result("owner-a", job_id))
                self.assertEqual(jobs.artifact("owner-a", job_id, "profile_analysis.md"), b"# Test conclusion")
                self.assertEqual(jobs.history("owner-b"), [])
                with self.assertRaises(ValueError):
                    jobs.artifact("owner-b", job_id, "profile_analysis.json")
                with self.assertRaises(ValueError):
                    jobs.artifact("owner-a", job_id, "../../.env")
            finally:
                jobs.pool.shutdown(wait=True)


    def test_resumed_legacy_export_collects_comments_before_analysis(self):
        for status in (None, "not_collected", "available", "limited", "unavailable", "rate_limited"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                calls = []
                class OfflineManager(JobManager):
                    def _process(self, job, script, arguments, env, config):
                        assert env["IG_INCLUDE_COMMENTS"] == "true"
                        assert env["IG_MAX_COMMENTS_PER_POST"] == "100"
                        assert int(env["OPENCODE_POST_MAX_OUTPUT_TOKENS"]) >= 2048
                        assert int(env["OPENCODE_MAX_MEDIA_PER_POST"]) >= 6
                        calls.append(script)
                        manifest = job.export_dir / "posts.json"
                        source = json.loads(manifest.read_text(encoding="utf-8"))
                        if script == "scrape_instagram.py":
                            assert arguments == ["--collect-comments", str(job.export_dir)]
                            source["posts"][0].update({"comments_status": "available", "comments": [{"text": "owner reply"}]})
                            manifest.write_text(json.dumps(source), encoding="utf-8")
                        else:
                            assert source["posts"][0]["comments_status"] != "not_collected"
                            (job.export_dir / "profile_analysis.json").write_text("{}", encoding="utf-8")
                export = Path(temporary) / "saved-export"
                export.mkdir()
                post = {"id": "1", "media_files": [{"file": "photo.jpg"}]}
                if status is not None:
                    post.update({"comments_status": status, "comments": []})
                (export / "posts.json").write_text(json.dumps({"posts": [post]}), encoding="utf-8")
                jobs = OfflineManager(Path(temporary) / "runtime")
                try:
                    job = Job("fixture", "owner-a", "target", export_dir=export)
                    jobs._run(job, {"IG_INCLUDE_COMMENTS": "false", "OPENCODE_MAX_MEDIA_PER_POST": "3",
                                    "OPENCODE_POST_MAX_OUTPUT_TOKENS": "1024"})
                    self.assertEqual(job.state, "complete", job.error)
                    expected = ["analyze_instagram_profile.py"]
                    if status in {None, "not_collected"}:
                        expected.insert(0, "scrape_instagram.py")
                    self.assertEqual(calls, expected)
                finally:
                    jobs.pool.shutdown(wait=True)

    def test_missing_comments_after_backfill_stops_before_paid_analysis(self):
        calls = []
        class OfflineManager(JobManager):
            def _process(self, job, script, arguments, env, config):
                calls.append(script)
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "saved-export"
            export.mkdir()
            (export / "posts.json").write_text(json.dumps({"posts": [{"id": "1", "media_files": [{"file": "photo.jpg"}]}]}), encoding="utf-8")
            jobs = OfflineManager(Path(temporary) / "runtime")
            try:
                job = Job("fixture", "owner-a", "target", export_dir=export)
                jobs._run(job, {})
                self.assertEqual(job.state, "failed")
                self.assertIn("збір коментарів", job.error)
                self.assertEqual(calls, ["scrape_instagram.py"])
            finally:
                jobs.pool.shutdown(wait=True)


class StreamlitSmokeTests(unittest.TestCase):
    def test_home_demo_and_history_render_without_model_requests(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=15).run()
        self.assertEqual(len(app.exception), 0)
        app.button(key="home_demo").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("dental_studio_demo", app.title[0].value)
        self.assertEqual(len(app.get("download_button")), 2)
        app.button(key="nav_history").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "Мої аналізи")


if __name__ == "__main__":
    unittest.main()
