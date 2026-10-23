from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scrape_instagram_scrapedo import (ScrapeDoClient, body_documents, cached_details, discover_posts,
    export_cached_posts, media_shortcode, normalized_post, public_comments)
from scrapedo_probe import fetch_url


def media(pk="3958471336240687550", **extra):
    return {"pk": pk, "media_type": 1, "caption": {"text": "Fictional caption"},
            "image_versions2": {"candidates": []}, **extra}


def page(nodes, cursor="next", query=True):
    doc = {"username": "example", "polaris_timeline_connection": {"edges": [{"node": n} for n in nodes],
        "page_info": {"has_next_page": bool(cursor), "end_cursor": cursor}}}
    if query:
        doc["preloader"] = {"queryName": "PolarisProfilePostsLoggedOutTabGridUIContentQuery",
            "queryID": "1234", "variables": {"first": 12, "user_id": "123"}}
    return '<meta property="og:description" content="10 Followers, 65 Posts"><script type="application/json">' + json.dumps(doc) + '</script>'


def metadata(target="https://www.instagram.com/example/", **extra):
    return {"target": target, "render": False, "http_status": 200,
            "authenticated_instagram_session": False, "credits_used": "1", **extra}


class ScrapeDoCollectionTests(unittest.TestCase):
    def test_cache_survives_restart_and_does_not_consume_budget(self):
        with tempfile.TemporaryDirectory() as directory, patch("scrape_instagram_scrapedo.fetch_url", return_value=(metadata(), "body")) as fetch:
            client = ScrapeDoClient("fictional", Path(directory), max_credits=1)
            client.fetch(metadata()["target"])
            client.fetch(metadata()["target"])
            restored = ScrapeDoClient("fictional", Path(directory), max_credits=0)
            self.assertEqual(restored.fetch(metadata()["target"])[1], "body")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(client.new_requests, 1)
            self.assertEqual(client.cache_hits, 1)
            self.assertEqual(restored.credits_used, 1)
            self.assertEqual(restored.new_requests, 0)
            self.assertNotIn("fictional", (Path(directory) / "requests.json").read_text())

    def test_budget_reserves_full_residential_and_render_price(self):
        with tempfile.TemporaryDirectory() as directory, patch("scrape_instagram_scrapedo.fetch_url") as fetch:
            client = ScrapeDoClient("fictional", Path(directory), max_credits=9)
            with self.assertRaisesRegex(RuntimeError, "budget"):
                client.fetch(metadata()["target"], options={"super": "true"})
            with self.assertRaisesRegex(RuntimeError, "budget"):
                client.fetch(metadata()["target"], render=True, options={"super": "true"})
            fetch.assert_not_called()

    def test_unknown_network_cost_is_reserved_instead_of_treated_as_free(self):
        with tempfile.TemporaryDirectory() as directory, patch("scrape_instagram_scrapedo.fetch_url", side_effect=RuntimeError("Network failed")) as fetch:
            client = ScrapeDoClient("fictional", Path(directory), max_credits=1)
            with self.assertRaises(RuntimeError):
                client.fetch(metadata()["target"])
            self.assertEqual(client.credits_used, 1)
            with self.assertRaisesRegex(RuntimeError, "budget"):
                client.fetch(metadata()["target"])
            self.assertEqual(fetch.call_count, 1)

    def test_failed_responses_are_cached_without_automatic_retries(self):
        with tempfile.TemporaryDirectory() as directory, patch("scrape_instagram_scrapedo.fetch_url", return_value=(metadata(http_status=502, credits_used="0"), "error")) as fetch:
            client = ScrapeDoClient("fictional", Path(directory))
            client.fetch(metadata()["target"])
            client.fetch(metadata()["target"])
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(client.credits_used, 0)

    def test_seed_requires_explicit_anonymous_response(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ScrapeDoClient("fictional", Path(directory))
            with self.assertRaises(ValueError):
                client.seed(metadata(authenticated_instagram_session=True), "body")
            client.seed(metadata(), "body")
            self.assertEqual(client.fetch(metadata()["target"])[1], "body")
            self.assertEqual(client.credits_used, 0)
            self.assertEqual(client.ledger, [])

    def test_numeric_shortcode_matches_verified_public_permalink(self):
        self.assertEqual(media_shortcode(media()), "DbvULUaMSm-")
        self.assertIsNone(media_shortcode({"pk": "not-numeric"}))
        self.assertIsNone(media_shortcode({"pk": str(2**65)}))
        self.assertEqual(media_shortcode({"code": "Example123"}), "Example123")

    def test_returnjson_envelope_exposes_both_html_and_network_json(self):
        value = {"content": page([media()]), "networkRequests": [
            {"response_body": json.dumps({"data": {"extra": 1}})}]}
        docs = body_documents(json.dumps(value))
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["username"], "example")
        self.assertEqual(docs[1]["data"]["extra"], 1)

    def test_comment_absence_differs_from_verified_zero(self):
        self.assertEqual(public_comments({}, "example"), ([], "unavailable"))
        self.assertEqual(public_comments({"comment_count": 0}, "example"), ([], "available"))

    def test_comment_deduplication_attribution_and_source_immutability(self):
        comment = {"pk": "1", "text": "Fictional owner reply", "user": {"username": "example"}}
        source = {"comments": [comment], "comment_count": 3,
            "edge_media_to_parent_comment": {"edges": [{"node": comment}]}}
        before = json.dumps(source)
        comments, status = public_comments(source, "example")
        self.assertEqual(status, "partial")
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["is_profile_owner"])
        self.assertEqual(json.dumps(source), before)

    def test_comments_limit_is_explicit(self):
        source = {"comment_count": 101, "comments": [{"pk": str(i), "text": "Fictional"} for i in range(101)]}
        comments, status = public_comments(source, "example")
        self.assertEqual(len(comments), 100)
        self.assertEqual(status, "limited")

    def test_public_replies_preserve_parent_context_and_iso_timestamps(self):
        source = {"comment_count": 2, "comments": [{"pk": "1", "text": "Fictional question", "user": {"username": "reader"},
            "preview_child_comments": [{"pk": "2", "text": "Fictional answer", "user": {"username": "example"}, "created_at": 1700000000}]}]}
        comments, status = public_comments(source, "example")
        self.assertEqual(status, "available")
        self.assertTrue(comments[1]["is_reply"])
        self.assertTrue(comments[1]["is_profile_owner"])
        self.assertEqual(comments[1]["parent_text"], "Fictional question")
        self.assertEqual(comments[1]["created_at"], "2023-11-14T22:13:20+00:00")

    def test_unknown_dates_are_never_inferred_from_numeric_media_ids(self):
        result = normalized_post(media(), "example")
        self.assertEqual(result["code"], "DbvULUaMSm-")
        self.assertNotIn("taken_at", result)
        self.assertEqual(result["comments_status"], "unavailable")

    def test_empty_pagination_stops_after_first_bulk_attempt(self):
        client = Mock()
        client.fetch.side_effect = [(metadata(), page([media()])), (metadata(), '{"data":{"xig_user_by_igid_v2":null}}')]
        nodes, advertised, reason = discover_posts(client, "example", 100)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(advertised, 65)
        self.assertEqual(reason, "pagination_returned_no_new_posts")
        self.assertEqual(client.fetch.call_count, 2)

    def test_duplicate_pagination_does_not_charge_for_an_infinite_loop(self):
        client = Mock()
        client.fetch.side_effect = [(metadata(), page([media()])), (metadata(), page([media()]))]
        nodes, _, reason = discover_posts(client, "example", 100)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(reason, "pagination_returned_no_new_posts")
        self.assertEqual(client.fetch.call_count, 2)

    def test_hundred_post_limit_never_counts_extra_nodes(self):
        client = Mock()
        client.fetch.return_value = (metadata(), page([media(str(i)) for i in range(1, 102)]))
        nodes, _, reason = discover_posts(client, "example", 100)
        self.assertEqual(len(nodes), 100)
        self.assertEqual(reason, "target_reached")
        self.assertEqual(client.fetch.call_count, 1)

    def test_no_next_page_is_recorded_as_source_exhausted(self):
        client = Mock()
        client.fetch.return_value = (metadata(), page([media()], cursor=None))
        _, _, reason = discover_posts(client, "example", 100)
        self.assertEqual(reason, "source_exhausted")
        self.assertEqual(client.fetch.call_count, 1)

    def test_public_bio_and_full_name_are_retained_for_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ScrapeDoClient("fictional", Path(directory))
            body = page([media()], cursor=None) + '<script type="application/json">' + json.dumps(
                {"username": "example", "full_name": "Fictional Name", "biography": "Fictional hobby: guitar"}) + '</script>'
            with patch.object(client, "fetch", return_value=(metadata(), body)):
                nodes, advertised, reason = discover_posts(client, "example", 100)
            export = export_cached_posts(client, "example", nodes, advertised, reason, download=False)
            self.assertEqual(export["account"]["biography"], "Fictional hobby: guitar")
            self.assertEqual(export["account"]["full_name"], "Fictional Name")

    def test_cached_post_details_require_matching_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ScrapeDoClient("fictional", Path(directory))
            client.seed(metadata("https://www.instagram.com/p/DbvULUaMSm-/"),
                json.dumps(media(code="DbvULUaMSm-", taken_at=1700000000, user={"username": "other"})))
            self.assertEqual(cached_details(client, "example"), {})
            self.assertEqual(len(cached_details(client, "other")), 1)

    def test_partial_export_retains_missing_coverage_and_video_preview_label(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ScrapeDoClient("fictional", Path(directory))
            export = export_cached_posts(client, "example", [media(media_type=2)], 65,
                "pagination_returned_no_new_posts", download=False)
            self.assertFalse(export["coverage"]["discovery_complete"])
            self.assertEqual(export["coverage"]["posts_with_dates"], 0)
            self.assertEqual(export["coverage"]["verified_comment_threads"], 0)
            self.assertEqual(export["posts"][0]["media_coverage"], "video_preview_only")
            self.assertTrue((Path(directory) / "posts.json").is_file())

    def test_export_resume_reuses_files_and_refreshes_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "media").mkdir()
            (folder / "media" / "sample.jpg").write_bytes(b"fixture")
            client = ScrapeDoClient("fictional", folder)
            old = {"shortcode": "DbvULUaMSm-", "caption": "stale", "media_files": [{"file": "sample.jpg", "bytes": 7}]}
            (folder / "posts.json").write_text(json.dumps({"posts": [old]}), encoding="utf-8")
            with patch("scrape_instagram_scrapedo.build_post_records", return_value=[]) as prepare:
                export = export_cached_posts(client, "example", [media()], 65, "source_exhausted")
            self.assertEqual(prepare.call_args.args[0], [])
            self.assertEqual(export["posts"][0]["caption"], "Fictional caption")
            self.assertEqual(export["coverage"]["media_files_saved"], 1)

    def test_authenticated_target_headers_are_rejected_before_network(self):
        with patch("scrapedo_probe.build_opener") as opener:
            with self.assertRaises(ValueError):
                fetch_url(metadata()["target"], "fictional", target_headers={"Cookie": "sessionid=fictional"})
            opener.assert_not_called()

    def test_cookie_options_and_target_overrides_are_case_insensitively_rejected(self):
        with patch("scrapedo_probe.build_opener") as opener:
            for options in ({"SETCOOKIES": "sessionid=fictional"}, {"URL": "https://other.example/"}, {"TOKEN": "other"}):
                with self.assertRaises(ValueError):
                    fetch_url(metadata()["target"], "fictional", options=options)
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
