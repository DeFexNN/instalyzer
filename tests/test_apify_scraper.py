from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scrape_instagram_apify import (ApifyClient, NoRedirects, collect_profile, collect_extra_comments,
    merge_comments, normalize_comments, normalize_post, timestamp)
from web_backend import Job, JobManager, collection_provider, live_configuration, result_cost


def actor_run(**extra):
    return {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1", "usageTotalUsd": .0027,
        "chargedEventCounts": {"result": 1}, "pricingInfo": {"pricingPerEvent": {"actorChargeEvents": {
            "result": {"eventPriceUsd": .0027}}}}, **extra}


def post(**extra):
    return {"id": "123", "shortCode": "Example123", "type": "Image", "caption": "Fictional caption",
        "timestamp": "2026-01-01T12:00:00.000Z", "displayUrl": "https://scontent.cdninstagram.com/a.jpg",
        "commentsCount": 0, "ownerUsername": "example", **extra}


class ApifyClientTests(unittest.TestCase):
    def test_completed_dataset_is_reused_after_restart_without_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            client = ApifyClient("fictional-token", folder)
            with patch.object(client, "request", side_effect=[{"data": actor_run()}, [post()]]) as req:
                self.assertEqual(len(client.run("posts", {"resultsLimit": 1})), 1)
                self.assertEqual(req.call_count, 2)
                self.assertEqual(req.call_args_list[0].kwargs["params"]["maxTotalChargeUsd"], .5)
            restored = ApifyClient("fictional-token", folder, max_total_charge_usd=.001)
            with patch.object(restored, "request", side_effect=AssertionError("Unexpected request")):
                self.assertEqual(len(restored.run("posts", {"resultsLimit": 1})), 1)
            self.assertEqual(restored.new_runs, 0)
            self.assertEqual(restored.cost_usd, .0027)
            self.assertNotIn("fictional-token", restored.state_path.read_text())

    def test_lost_start_response_never_retries_the_paid_post(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ApifyClient("fictional", Path(directory))
            with patch.object(client, "request", side_effect=RuntimeError("Network error")) as req:
                with self.assertRaises(RuntimeError):
                    client.run("posts", {})
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    client.run("posts", {})
            self.assertEqual(req.call_count, 1)

    def test_delayed_billing_is_settled_before_reallocating_remaining_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ApifyClient("fictional", Path(directory))
            delayed = actor_run(usageTotalUsd=0, chargedEventCounts={"result": 0})
            with patch.object(client, "request", side_effect=[{"data": delayed}, [post()], {"data": actor_run()},
                    {"data": actor_run(id="run2")}, [{"username": "example"}]]) as req:
                client.run("posts", {})
                self.assertEqual(client.cost_usd, .0027)
                client.run("profile", {"resultsType": "details"})
            self.assertAlmostEqual(req.call_args_list[3].kwargs["params"]["maxTotalChargeUsd"], .4973)
            self.assertEqual(client.cost_usd, .0054)

    def test_unknown_billing_blocks_another_paid_run(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ApifyClient("fictional", Path(directory))
            client.runs["other"] = {"usageTotalUsd": .01, "billing_settled": False}
            with patch.object(client, "request") as req, self.assertRaisesRegex(RuntimeError, "cost is not yet known"):
                client.run("posts", {})
            req.assert_not_called()

    def test_failed_run_is_not_started_again_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ApifyClient("fictional", Path(directory))
            with patch.object(client, "request", return_value={"data": actor_run(status="FAILED")}) as req:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "FAILED"):
                        client.run("posts", {})
            self.assertEqual(req.call_count, 1)

    def test_poll_failure_resumes_the_same_saved_run_without_another_post(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            client = ApifyClient("fictional", folder)
            with patch.object(client, "request", side_effect=[{"data": actor_run(status="RUNNING")}, RuntimeError("Temporary outage")]):
                with self.assertRaisesRegex(RuntimeError, "Temporary outage"):
                    client.run("posts", {})
            restored = ApifyClient("fictional", folder)
            with patch.object(restored, "request", side_effect=[{"data": actor_run()}, [post()]]) as req:
                self.assertEqual(len(restored.run("posts", {})), 1)
            self.assertEqual(req.call_args_list[0].args[0], "/actor-runs/run1")
            self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in req.call_args_list))
            self.assertEqual(restored.new_runs, 0)

    def test_request_uses_bearer_header_and_redacts_the_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"echo":"fictional-secret"}'
        opener = Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch("scrape_instagram_apify.build_opener", return_value=opener):
            client = ApifyClient("fictional-secret", Path(directory))
            value = client.request("/actor-runs/run1")
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer fictional-secret")
        self.assertNotIn("fictional-secret", req.full_url)
        self.assertEqual(value["echo"], "[redacted]")
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://other.example"))

    def test_http_error_diagnostics_do_not_include_credentials(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("secret-bearing-url", 401, "fictional-secret", {}, None)
        with tempfile.TemporaryDirectory() as directory, patch("scrape_instagram_apify.build_opener", return_value=opener):
            client = ApifyClient("fictional-secret", Path(directory))
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as error:
                client.request("/actor-runs/run1")
            self.assertNotIn("fictional-secret", str(error.exception))

    def test_abort_only_targets_pending_saved_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ApifyClient("fictional", Path(directory))
            client.runs = {"a": actor_run(status="RUNNING"), "b": actor_run(id="completed")}
            with patch.object(client, "request", return_value={"data": actor_run(status="ABORTED")}) as req:
                client.abort_pending()
            self.assertEqual(req.call_count, 1)
            self.assertEqual(req.call_args.args[0], "/actor-runs/run1/abort")


class ApifyNormalizationTests(unittest.TestCase):
    def test_carousel_images_and_video_sources_keep_all_children(self):
        item = post(type="Sidecar", childPosts=[post(id="a"), post(id="b", type="Video", videoUrl="https://scontent.cdninstagram.com/b.mp4")])
        normalized = normalize_post(item, "example")
        self.assertEqual(len(normalized["carousel_media"]), 2)
        self.assertTrue(normalized["carousel_media"][1]["is_video"])
        self.assertEqual(len(normalized["carousel_media"][1]["video_versions"]), 1)
        self.assertEqual(normalized["comments_status"], "available")

    def test_sidecar_falls_back_to_images_without_adding_the_cover_twice(self):
        normalized = normalize_post(post(type="Sidecar", images=["https://scontent.cdninstagram.com/a.jpg", "https://scontent.cdninstagram.com/b.jpg"]), "example")
        self.assertEqual(len(normalized["carousel_media"]), 2)

    def test_unknown_date_and_comment_text_do_not_become_verified_empty(self):
        normalized = normalize_post(post(timestamp=None, commentsCount=3, firstComment="No attributed source"), "example")
        self.assertIsNone(normalized["taken_at"])
        self.assertEqual(normalized["comments_status"], "unavailable")
        self.assertIsNone(timestamp("bad date"))
        self.assertIsNone(normalize_post({"error": "not_found"}, "example"))

    def test_comment_owner_reply_and_parent_context_are_preserved(self):
        items = [{"id": "parent", "text": "Fictional question", "ownerUsername": "reader", "replies": [
            {"id": "reply", "text": "Fictional answer", "ownerUsername": "example", "timestamp": "2026-01-01T00:00:00Z"}]}]
        comments = normalize_comments(items + items, "example")
        self.assertEqual(len(comments), 2)
        self.assertTrue(comments[1]["is_profile_owner"])
        self.assertTrue(comments[1]["is_reply"])
        self.assertEqual(comments[1]["parent_text"], "Fictional question")
        self.assertEqual(comments[1]["created_at"], "2026-01-01T00:00:00+00:00")

    def test_duplicate_parent_can_add_replies_missing_from_its_first_copy(self):
        parent = {"id": "parent", "text": "Fictional parent"}
        comments = normalize_comments([parent, {**parent, "replies": [{"id": "reply", "text": "Fictional reply"}]}], "example")
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[1]["parent_text"], "Fictional parent")

    def test_merge_can_fill_missing_context_without_mutating_the_sources(self):
        old = {"id": "reply", "text": "Fictional reply", "parent_text": None}
        result = merge_comments([old], [{**old, "parent_text": "Fictional question"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["parent_text"], "Fictional question")
        self.assertIsNone(old["parent_text"])

    def test_bulk_comments_map_to_their_post_and_merge_without_duplicates(self):
        source = post(commentsCount=2, latestComments=[{"id": "1", "text": "first", "ownerUsername": "reader"}])
        normalized = normalize_post(source, "example")
        client = Mock()
        client.run.return_value = [{"postUrl": "https://www.instagram.com/p/Example123/", "id": "1", "text": "first"},
                                  {"postUrl": "https://www.instagram.com/p/Example123/", "id": "2", "text": "second"}]
        collect_extra_comments(client, [normalized], "example")
        self.assertEqual(len(normalized["exported_comments"]), 2)
        self.assertEqual(normalized["comments_status"], "available")
        self.assertEqual(len(client.run.call_args.args[1]["directUrls"]), 1)

    def test_export_resume_reuses_files_updates_metadata_and_reports_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "media").mkdir()
            (folder / "media" / "a.jpg").write_bytes(b"fixture")
            (folder / "posts.json").write_text(json.dumps({"posts": [{"shortcode": "Example123", "caption": "old", "media_files": [{"file": "a.jpg", "bytes": 7}]}]}))
            client = ApifyClient("fictional", folder)
            with patch.object(client, "run", side_effect=[[post()], [{"username": "example", "postsCount": 65, "biography": "Fictional hobby"}]]), \
                    patch("scrape_instagram_apify.build_post_records", return_value=[]) as prepare:
                export = collect_profile(client, "example")
            self.assertEqual(prepare.call_args.args[0], [])
            self.assertEqual(export["posts"][0]["caption"], "Fictional caption")
            self.assertEqual(export["account"]["biography"], "Fictional hobby")
            self.assertFalse(export["coverage"]["discovery_complete"])
            self.assertEqual(export["coverage"]["media_files_saved"], 1)

    def test_preview_rerun_keeps_comments_collected_in_an_earlier_full_run(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ApifyClient("fictional", Path(directory))
            saved = {"posts": [{"shortcode": "Example123", "comments": [{"id": "old", "text": "Saved source"}]}]}
            (Path(directory) / "posts.json").write_text(json.dumps(saved))
            with patch.object(client, "run", side_effect=[[post(commentsCount=1)], [{"username": "example"}]]):
                export = collect_profile(client, "example", download=False)
            self.assertEqual(export["posts"][0]["comments"][0]["text"], "Saved source")
            self.assertEqual(export["posts"][0]["comments_status"], "available")


class ApifyWebTests(unittest.TestCase):
    def test_invalid_provider_setting_does_not_crash_the_web_page(self):
        from streamlit.testing.v1 import AppTest
        with patch("web_backend.read_config", return_value={"INSTAGRAM_SCRAPER_PROVIDER": "invalid"}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=15).run()
        self.assertEqual(len(app.exception), 0)

    def test_apify_configuration_does_not_require_instagram_cookies(self):
        config = {"APP_PASSWORD": "a-long-demo-password", "APIFY_API_TOKEN": "fictional", "OPENCODE_API_KEY": "fictional"}
        self.assertEqual(collection_provider(config), "apify")
        self.assertEqual(live_configuration(config), (True, []))
        self.assertFalse(live_configuration({**config, "INSTAGRAM_SCRAPER_PROVIDER": "instagram"})[0])
        self.assertFalse(live_configuration({**config, "INSTAGRAM_SCRAPER_PROVIDER": "unknown"})[0])

    def test_source_cost_is_added_once_and_unknown_source_cost_is_not_free(self):
        payload = {"model_usage": {"cost_usd": .01}, "collection_usage": {"provider": "Apify", "cost_usd": .1}}
        self.assertAlmostEqual(result_cost(payload), .11)
        payload["collection_usage"]["cost_usd"] = None
        self.assertIsNone(result_cost(payload))

    def test_web_pipeline_selects_apify_and_keeps_usage_without_cookie_fallback(self):
        calls = []
        class OfflineManager(JobManager):
            def _process(self, job, script, arguments, env, config):
                calls.append(script)
                if script == "scrape_instagram_apify.py":
                    self.assertions = ("--limit" in arguments, "APIFY_API_TOKEN" in env)
                    export = Path(env["IG_OUTPUT_DIR"]) / "fixture"
                    export.mkdir(parents=True)
                    (export / "posts.json").write_text(json.dumps({"provider": "Apify", "posts": [{"comments": [], "comments_status": "available", "media_files": [{"file": "a.jpg"}]}],
                        "provider_usage": {"provider": "Apify", "cost_usd": .1}, "coverage": {"collected_posts": 1}}))
                else:
                    (Path(arguments[0]) / "profile_analysis.json").write_text('{"model_usage":{"cost_usd":0.01}}')
        with tempfile.TemporaryDirectory() as directory:
            manager = OfflineManager(Path(directory))
            try:
                job = Job("fixture", "owner", "example")
                manager._run(job, {"APIFY_API_TOKEN": "fictional"})
                self.assertEqual(job.state, "complete", job.error)
                self.assertEqual(calls, ["scrape_instagram_apify.py", "analyze_instagram_profile.py"])
                self.assertEqual(manager.assertions, (True, True))
                report = json.loads((job.export_dir / "profile_analysis.json").read_text())
                self.assertAlmostEqual(result_cost(report), .11)
                self.assertEqual(report["web_execution"]["scraper_provider"], "apify")
            finally:
                manager.pool.shutdown(wait=True)

    def test_saved_remote_run_can_resume_before_manifest_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(Path(directory))
            try:
                state = Path(directory) / "fixture" / "exports" / "instagram-example-apify" / ".apify" / "runs.json"
                state.parent.mkdir(parents=True)
                state.write_text("{}")
                job = Job("fixture", "owner", "example", state="failed")
                manager.jobs[job.id] = job
                self.assertTrue(manager.snapshot("owner", job.id)["can_resume"])
            finally:
                manager.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
