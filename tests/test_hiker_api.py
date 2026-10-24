import unittest

from scrape_instagram_hiker import fetch_all_posts
from web_backend import collection_provider, live_configuration


def make_node(code, taken_at=1700000000):
    return {
        "code": code,
        "pk": code,
        "media_type": 1,
        "taken_at": taken_at,
        "caption": {"text": f"caption {code}"},
        "user": {"username": "demo"},
        "image_versions2": {"candidates": [
            {"url": "https://scontent.cdninstagram.com/example.jpg", "width": 100, "height": 100}
        ]},
    }


def make_page(codes, next_cursor=None):
    connection = {
        "edges": [{"node": make_node(code)} for code in codes],
        "page_info": {"has_next_page": bool(next_cursor), "end_cursor": next_cursor},
    }
    return {
        "response": {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": connection}},
        "next_page_id": next_cursor,
    }


class FakeHikerClient:
    base_url = "https://api.instagrapi.com"
    requests = 0
    cached = 0

    def __init__(self):
        self.pages = {
            None: make_page(["a", "b", "c"], "cursor-2"),
            "cursor-2": make_page(["b", "c", "d"], "cursor-3"),
            "cursor-3": make_page(["e"], None),
        }
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/v2/user/by/username":
            return {"user": {"username": "demo", "media_count": 5}}
        return self.pages[params.get("page_id")]


class HikerPaginationTests(unittest.TestCase):
    def test_follows_cursor_and_deduplicates_posts(self):
        client = FakeHikerClient()
        posts, exhausted, reason = fetch_all_posts(client, "demo", limit=10)
        self.assertEqual([post["code"] for post in posts], ["a", "b", "c", "d", "e"])
        self.assertTrue(exhausted)
        self.assertEqual(reason, "source_exhausted")
        self.assertEqual([call[1].get("page_id") for call in client.calls],
                         [None, "cursor-2", "cursor-3"])

    def test_stops_at_requested_limit(self):
        client = FakeHikerClient()
        posts, exhausted, reason = fetch_all_posts(client, "demo", limit=2)
        self.assertEqual([post["code"] for post in posts], ["a", "b"])
        self.assertFalse(exhausted)
        self.assertEqual(reason, "target_reached")


class HikerProviderTests(unittest.TestCase):
    def test_auto_prefers_hiker_when_token_is_present(self):
        config = {"HIKERAPI_TOKEN": "fictional", "OPENCODE_API_KEY": "fictional",
                  "APP_PASSWORD": "a-long-demo-password"}
        self.assertEqual(collection_provider(config), "hiker")
        self.assertEqual(live_configuration(config), (True, []))

    def test_explicit_apify_still_wins_over_hiker_token(self):
        config = {"HIKERAPI_TOKEN": "fictional", "APIFY_API_TOKEN": "fictional",
                  "OPENCODE_API_KEY": "fictional", "APP_PASSWORD": "a-long-demo-password",
                  "INSTAGRAM_SCRAPER_PROVIDER": "apify"}
        self.assertEqual(collection_provider(config), "apify")

    def test_hiker_requires_token(self):
        config = {"OPENCODE_API_KEY": "fictional", "APP_PASSWORD": "a-long-demo-password",
                  "INSTAGRAM_SCRAPER_PROVIDER": "hiker"}
        ready, missing = live_configuration(config)
        self.assertFalse(ready)
        self.assertIn("HIKERAPI_TOKEN", missing)


if __name__ == "__main__":
    unittest.main()
