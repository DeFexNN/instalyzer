from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analyze_instagram_profile as analyzer
from analysis_prompts import (
    PROMPT_VERSION, build_post_prompt, build_synthesis_prompt, prepare_comments,
    synthesis_comment_evidence,
)


class EvidenceContextTests(unittest.TestCase):
    def test_original_audience_claims_and_dates_survive_both_model_passes(self):
        post = {"id": "1", "taken_at": "2022-05-01T12:00:00+00:00", "caption": "❤️",
            "media_files": [], "comments_status": "available", "comments": [
                {"text": "Гарне фото! Забрав мою сестру!", "is_profile_owner": False,
                 "created_at": "2022-05-02T13:00:00+00:00"},
                {"text": "Добре, що ми перестали спілкуватися.", "is_profile_owner": False,
                 "created_at": "2024-01-15T09:00:00+00:00"}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(analyzer, "call_model", return_value=("Visible scene; claims unverified.", {})) as model:
            result = analyzer.analyze_post("unused", "test", "offline", Path(folder), post, 1, 6, "minimal", 2048)
            post_input = model.call_args.args[3][0]["text"]
            analyzer.synthesize_profile("unused", "test", "offline", "example", [result])
            summary_input = model.call_args.args[3][0]["text"]
            evidence = json.loads(summary_input.split("ДАНІ:\n", 1)[1])[0]
            self.assertEqual(result["taken_at"], post["taken_at"])
            self.assertEqual(evidence["taken_at"], post["taken_at"])
            for comment in post["comments"]:
                self.assertIn(comment["text"], post_input)
                self.assertIn(comment["text"], summary_input)
                self.assertIn(comment["created_at"], summary_input)
            self.assertTrue(all(c["author_role"] == "other_commenter" for c in evidence["original_comment_evidence"]["quotes"]))
            self.assertEqual(evidence["author_comment_statements"], [])

    def test_synthesis_orders_dated_posts_and_keeps_unknown_dates_explicit(self):
        records = [
            {"analysis": "later", "taken_at": "2024-08-12T12:00:00+00:00"},
            {"analysis": "unknown"},
            {"analysis": "earlier", "taken_at": "2022-04-30T12:00:00+00:00"},
            {"analysis": "invalid", "taken_at": "not-a-date"}]
        evidence = json.loads(build_synthesis_prompt("example", records).split("ДАНІ:\n", 1)[1])
        self.assertEqual([e["analysis"] for e in evidence], ["earlier", "later", "unknown", "invalid"])
        self.assertIsNone(evidence[2]["taken_at"])
        self.assertEqual(evidence[3]["taken_at"], "not-a-date")
        self.assertEqual(evidence[0]["original_comment_evidence"]["coverage"], "not_saved_in_legacy_analysis")

    def test_quote_budget_keeps_both_roles_and_discloses_omitted_text(self):
        comments = [{"text": "Дякую!", "is_profile_owner": True} for _ in range(15)]
        comments.extend([{"text": "Уточнюю свій попередній коментар: це була спільна подорож.", "is_profile_owner": False},
                         {"text": "Довгий змістовний коментар. " * 50, "is_profile_owner": False}])
        context = prepare_comments({"comments": comments})
        evidence = synthesis_comment_evidence(context, max_comments=8, char_budget=500)
        self.assertEqual(evidence["quoted"], 8)
        self.assertEqual(evidence["not_quoted"], 9)
        self.assertEqual({c["author_role"] for c in evidence["quotes"]}, {"profile_owner", "other_commenter"})
        self.assertLessEqual(sum(len(c["text"]) + len(c.get("reply_to_text") or "") for c in evidence["quotes"]), 500)
        self.assertGreater(evidence["texts_truncated"], 0)

    def test_comment_dates_are_not_invented_and_reply_context_is_preserved(self):
        context = prepare_comments({"comments": [{"text": "Так, дякую за уточнення.", "is_profile_owner": True,
            "is_reply": True, "parent_text": "Тут ідеться про ваше навчання?"}]})
        quote = synthesis_comment_evidence(context)["quotes"][0]
        self.assertIsNone(quote["created_at"])
        self.assertTrue(quote["is_reply"])
        self.assertEqual(quote["reply_to_text"], "Тут ідеться про ваше навчання?")

    def test_changing_post_date_invalidates_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            post = {"id": "1", "caption": "Example", "media_files": [], "taken_at": "2022-01-01"}
            before = analyzer.checkpoint_fingerprint(Path(folder), [post], "example", "test", "minimal", 6, 2048)
            post["taken_at"] = "2024-01-01"
            after = analyzer.checkpoint_fingerprint(Path(folder), [post], "example", "test", "minimal", 6, 2048)
            self.assertNotEqual(before, after)
            self.assertGreater(PROMPT_VERSION, 4)

    def test_prompts_require_concrete_observations_without_inventing_relationship_status(self):
        post_prompt = build_post_prompt("", prepare_comments({}), media_attached=0,
            media_total=0, media_selected=0, taken_at=None)
        summary_prompt = build_synthesis_prompt("example", [{"analysis": "example"}])
        self.assertIn("обійми, поцілунок", post_prompt)
        self.assertIn("ХТО про КОГО і ЩО сказав", post_prompt)
        self.assertIn("не означає розрив людей на фото", post_prompt)
        self.assertIn("не встановлюють теперішній статус", summary_prompt)
        self.assertIn("не дописуй історію романтичного розриву", summary_prompt)
        self.assertNotIn("Без явного тексту описуй лише сцену та невідомість", post_prompt)


if __name__ == "__main__":
    unittest.main()
