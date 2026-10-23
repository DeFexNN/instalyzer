from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import analyze_instagram_profile as analyzer
import scrape_instagram as scraper
from analysis_prompts import MAX_POSTS, build_synthesis_prompt, comments_need_collection, prepare_comments


class DetailPipelineTests(unittest.TestCase):
    def test_feed_capture_preserves_one_hundred_posts(self):
        session = scraper.InstagramSession.__new__(scraper.InstagramSession)
        session.posts_by_id = {}
        session.feed_response_count = 0
        session.seen_cursors = set()
        response = Mock()
        response.url = "https://www.instagram.com/api/graphql"
        response.text.return_value = json.dumps({"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {
            "edges": [{"node": {"pk": str(i), "media_type": 1}} for i in range(1, 106)],
            "page_info": {"has_next_page": True, "end_cursor": "cursor"}}}})
        session._capture_feed_response(response)
        self.assertEqual(len(session.posts_by_id), 100)
        self.assertIn("100", session.posts_by_id)
        self.assertNotIn("101", session.posts_by_id)
        self.assertFalse(session.has_next_page)

    def test_one_hundred_posts_reach_synthesis(self):
        records = [{"analysis": f"evidence-{i}", "caption": ""} for i in range(1, 102)]
        text = build_synthesis_prompt("example", records)
        evidence = json.loads(text.split("ДАНІ:\n", 1)[1])
        self.assertEqual(MAX_POSTS, 100)
        self.assertEqual(scraper.MAX_POSTS, 100)
        self.assertEqual(analyzer.MAX_POSTS, 100)
        self.assertEqual(len(evidence), 100)
        self.assertEqual(evidence[-1]["analysis"], "evidence-100")

    def test_comments_and_owner_reply_reach_model_and_invalidate_checkpoint(self):
        post = {"id": "1", "caption": "Я працюю стоматологом. У вихідні граю на гітарі.", "media_files": [],
            "comments_status": "available", "comments": [
                {"username": "viewer", "text": "Це ваша дружина?", "is_profile_owner": False},
                {"username": "owner", "text": "Так, це моя дружина.", "is_profile_owner": True,
                 "is_reply": True, "parent_text": "Це ваша дружина?"}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(analyzer, "call_model", return_value=("details", {})) as model:
            export = Path(folder)
            before = analyzer.checkpoint_fingerprint(export, [post], "owner", "test", "minimal", 3, 2048)
            result = analyzer.analyze_post("unused", "test", "offline", export, post, 1, 3, "minimal", 2048)
            prompt = model.call_args.args[3][0]["text"]
            self.assertIn("Так, це моя дружина.", prompt)
            self.assertIn("граю на гітарі", prompt)
            self.assertIn('"author_role": "profile_owner"', prompt)
            self.assertEqual(result["comments_analyzed"], 2)
            self.assertIn("Так, це моя дружина.", build_synthesis_prompt("owner", [result]))
            post["comments"][1]["text"] = "Ні, це колега."
            after = analyzer.checkpoint_fingerprint(export, [post], "owner", "test", "minimal", 3, 2048)
            self.assertNotEqual(before, after)

    def test_comment_budget_keeps_owner_reply_and_reports_missing_context(self):
        prepared = prepare_comments({"comments": [
            {"text": "x" * 200, "is_profile_owner": False},
            {"text": "Я займаюся плаванням.", "is_profile_owner": True}]}, max_comments=1, char_budget=100)
        self.assertEqual(prepared["comments"][0]["author_role"], "profile_owner")
        self.assertEqual(prepared["sampled_out"], 1)
        missing = prepare_comments({"caption": "no comments collected"})
        self.assertEqual(missing["collection_status"], "not_collected")

    def test_comment_replies_keep_parent_context_and_target_author_role(self):
        session = scraper.InstagramSession.__new__(scraper.InstagramSession)
        session.get_comments_json = Mock(return_value={"comments": [{"pk": "c1", "text": "Це ваша дружина?",
            "user": {"username": "viewer"}, "preview_child_comments": [{"pk": "c2", "text": "Так, моя дружина.",
            "user": {"username": "target_profile"}}]}], "next_min_id": "next"})
        comments = session.all_comments("post", "target_profile", max_comments=2)
        self.assertEqual(len(comments), 2)
        self.assertTrue(comments[1]["is_profile_owner"])
        self.assertEqual(comments[1]["parent_text"], "Це ваша дружина?")
        self.assertTrue(session.comments_limit_reached)

    def test_rate_limited_comments_do_not_abort_posts_or_repeat_requests(self):
        session = Mock()
        session.all_comments.side_effect = RuntimeError("HTTP 429")
        posts = [{"pk": "one"}, {"pk": "two"}]
        scraper.collect_post_comments(session, posts, "target")
        session.all_comments.assert_called_once()
        self.assertEqual([p["comments_status"] for p in posts], ["rate_limited", "rate_limited"])
        record = scraper.post_to_record(posts[0], 1, Path("unused"), False, True)
        self.assertEqual(record["comments_status"], "rate_limited")
        self.assertEqual(record["comments"], [])

    def test_missing_collection_is_distinct_from_empty_and_unavailable_threads(self):
        self.assertTrue(comments_need_collection({}))
        self.assertTrue(comments_need_collection({"comments_status": "not_collected", "comments": []}))
        self.assertTrue(comments_need_collection({"comments_status": "available"}))
        for status in ("available", "limited", "unavailable", "rate_limited"):
            self.assertFalse(comments_need_collection({"comments_status": status, "comments": []}))

    def test_backfill_preserves_media_and_previously_collected_threads(self):
        with tempfile.TemporaryDirectory() as folder:
            export = Path(folder)
            media = export / "photo.jpg"
            media.write_bytes(b"original-image")
            existing = {"id": "2", "comments_status": "available", "comments": [], "caption": "already collected"}
            source = {"account": {"username": "target"}, "comments_included": False, "posts": [
                {"id": "1", "caption": "keep caption", "media_files": [{"file": "photo.jpg"}]}, existing]}
            manifest = export / "posts.json"
            manifest.write_text(json.dumps(source), encoding="utf-8")
            session = Mock()
            session.current_user.return_value = {"username": "owner"}
            session.comments_limit_reached = False
            session.all_comments.return_value = [{"text": "owner clarification", "is_profile_owner": True}]
            with patch.object(scraper, "InstagramSession", return_value=session):
                scraper.collect_export_comments(export, "owner")
            result = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertTrue(result["comments_included"])
            self.assertEqual(result["posts"][0]["comments_status"], "available")
            self.assertEqual(result["posts"][0]["comments"][0]["text"], "owner clarification")
            self.assertEqual(result["posts"][0]["caption"], "keep caption")
            self.assertEqual(result["posts"][0]["media_files"], source["posts"][0]["media_files"])
            self.assertEqual(media.read_bytes(), b"original-image")
            self.assertEqual(result["posts"][1], existing)
            session.all_comments.assert_called_once_with("1", "target", scraper.comment_limit())
            session.prepare_profile.assert_called_once_with("target")
            session.close.assert_called_once()
            with patch.object(scraper, "InstagramSession") as factory:
                scraper.collect_export_comments(export, "owner")
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
