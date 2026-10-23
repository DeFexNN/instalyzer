from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote_plus, urlparse
from unittest.mock import Mock, patch

from scrapedo_probe import NoRedirects, export_sample, fetch_profile, inspect_body, post_shortcode


class ScrapeDoProbeTests(unittest.TestCase):
    def fixture(self):
        single = {"pk": "1", "media_type": 1, "caption": {"text": "Fictional caption"},
                  "image_versions2": {"candidates": []}, "seo_canonical_url": None}
        carousel = {"pk": "2", "media_type": 8, "caption": {"text": "Fictional carousel"},
                    "carousel_media": [{"pk": "3", "media_type": 1}, {"pk": "4", "media_type": 1}]}
        return {"data": {"xig_user_by_igid_v2": {"username": "example", "polaris_timeline_connection": {
            "edges": [{"node": single}, {"node": carousel}],
            "page_info": {"has_next_page": True, "end_cursor": "sample-cursor"}}}}}

    def test_embedded_logged_out_feed_is_parsed_without_shortcode_and_without_counting_carousel_children(self):
        data = self.fixture()
        html = '<title>Example profile</title><script type="application/json">' + json.dumps(data) + '</script>'
        result = inspect_body(html, "example")
        self.assertEqual(result["post_count"], 2)
        self.assertTrue(result["profile_found"])
        self.assertTrue(result["has_next_page"])
        self.assertEqual(result["post_nodes"][0]["caption"]["text"], "Fictional caption")
        self.assertIsNone(result["post_nodes"][0]["shortcode"])

    def test_json_response_and_duplicate_nodes_are_supported(self):
        fixture = self.fixture()
        connection = fixture["data"]["xig_user_by_igid_v2"]["polaris_timeline_connection"]
        connection["edges"].append(connection["edges"][0])
        self.assertEqual(inspect_body(json.dumps(fixture), "example")["post_count"], 2)

    def test_login_page_is_not_a_successful_feed(self):
        html = '<title>Instagram</title><input name="username"><input name="password">'
        result = inspect_body(html, "example")
        self.assertEqual(result["post_count"], 0)
        self.assertTrue(result["login_form_present"])
        self.assertFalse(result["profile_found"])

    def test_missing_cursor_does_not_mean_the_source_is_exhausted(self):
        fixture = self.fixture()
        fixture["data"]["xig_user_by_igid_v2"]["polaris_timeline_connection"]["page_info"]["end_cursor"] = None
        result = inspect_body(json.dumps(fixture), "example")
        self.assertTrue(result["has_next_page"])
        self.assertIsNone(result["next_cursor"])

    def test_request_sends_only_provider_key_and_redacts_it_in_response(self):
        secret = "fictional+key with spaces"
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.headers = {"Content-Type": "text/html", "Scrape.do-Request-Cost": "1", "Scrape.do-Remaining-Credits": "999"}
        response.read.return_value = (secret + " " + quote_plus(secret)).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch("scrapedo_probe.build_opener", return_value=opener), patch.dict(os.environ, {"IG_SESSIONID": "must-never-be-transferred"}):
            metadata, body = fetch_profile("example", secret)
        request = opener.open.call_args.args[0]
        self.assertEqual(urlparse(request.full_url).netloc, "api.scrape.do")
        parameters = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(parameters["token"], [secret])
        self.assertEqual(parameters["disableRetry"], ["true"])
        self.assertNotIn("setCookies", parameters)
        self.assertNotIn("Cookie", dict(request.header_items()))
        self.assertNotIn("must-never-be-transferred", request.full_url)
        self.assertNotIn(secret, body)
        self.assertNotIn(quote_plus(secret), body)
        self.assertEqual(metadata["credits_used"], "1")
        self.assertFalse(metadata["authenticated_instagram_session"])

    def test_network_errors_do_not_expose_request_url_or_key(self):
        opener = Mock()
        opener.open.side_effect = URLError("secret-token-bearing-url")
        with patch("scrapedo_probe.build_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as raised:
                fetch_profile("example", "secret-token")
        self.assertNotIn("secret-token", str(raised.exception))

    def test_provider_redirect_is_not_followed(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://other.example/"))

    def test_canonical_post_links_only_accept_instagram(self):
        self.assertEqual(post_shortcode({"seo_canonical_url": "https://www.instagram.com/example/p/ABCdef123/"}), "ABCdef123")
        self.assertIsNone(post_shortcode({"seo_canonical_url": "https://other.example/p/ABCdef123/"}))

    def test_manifest_keeps_caption_unknown_dates_and_explicit_comment_coverage(self):
        result = inspect_body(json.dumps(self.fixture()), "example")
        with tempfile.TemporaryDirectory() as folder:
            export = export_sample(Path(folder), "example", result)
            self.assertEqual(export["post_count"], 2)
            self.assertEqual(export["posts"][0]["caption"], "Fictional caption")
            self.assertIsNone(export["posts"][0]["taken_at"])
            self.assertEqual(export["posts"][1]["source_media_count"], 2)
            self.assertFalse(export["comments_included"])
            self.assertEqual(export["posts"][0]["comments_status"], "not_collected")
            self.assertTrue((Path(folder) / "posts.json").is_file())


if __name__ == "__main__":
    unittest.main()
