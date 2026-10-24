"""Collect a public Instagram profile through HikerAPI without an Instagram session.

The provider keeps its own session pool; this module only talks to
``api.instagrapi.com`` / ``api.hikerapi.com`` and never forwards Instagram
cookies. Responses are cached under ``.runtime/hiker/<username>/`` so a replay
does not spend provider requests again.

Usage::

    python scrape_instagram_hiker.py bohdankryuk --limit 65
    python scrape_instagram_hiker.py bohdankryuk --limit 65 --no-download
    python scrape_instagram_hiker.py bohdankryuk --comments --max-comments 50
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis_prompts import DEFAULT_MAX_COMMENTS, MAX_POSTS
from scrape_instagram import (
    ROOT,
    InstagramSession,
    build_post_records,
    post_to_record,
    profile_username_from_input,
)

ENV_FILE = ROOT / ".env"
DEFAULT_BASE_URL = "https://api.instagrapi.com"
TOKEN_ENV = "HIKERAPI_TOKEN"
DEFAULT_MAX_REQUESTS = 40
REQUEST_TIMEOUT = 60


class HikerError(RuntimeError):
    """Raised when HikerAPI rejects a request or returns an unusable payload."""


class HikerRequestCapReached(HikerError):
    """Raised before a request that would exceed the configured paid-request cap."""


def load_env() -> None:
    """Read .env when present without requiring Instagram session variables."""
    if not ENV_FILE.is_file():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class HikerClient:
    """Minimal HikerAPI client with an on-disk response cache."""

    def __init__(self, token: str, cache_dir: Path, *, max_requests: int = DEFAULT_MAX_REQUESTS,
                 refresh: bool = False, base_url: str | None = None):
        if not token:
            raise RuntimeError(f"Set {TOKEN_ENV} in .env or environment variables.")
        if max_requests < 1:
            raise ValueError("HikerAPI max requests must be positive.")
        self.token = token
        self.base_url = (base_url or os.getenv("HIKERAPI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.cache_dir = cache_dir
        self.max_requests = max_requests
        self.refresh = refresh
        self.requests = 0
        self.cached = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path:
        key = hashlib.sha256(
            json.dumps([path, params], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        cache_path = self._cache_path(path, params)
        if cache_path.is_file() and not self.refresh:
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self.cached += 1
                    return payload
            except (OSError, ValueError):
                pass

        if self.requests >= self.max_requests:
            raise HikerRequestCapReached(
                f"HikerAPI request cap reached ({self.max_requests}); no further paid requests were made."
            )

        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "x-access-key": self.token,
                "accept": "application/json",
                "user-agent": "instalyzer-hiker/1.0",
            },
            method="GET",
        )
        payload = self._request_with_retry(request)
        if not isinstance(payload, dict):
            raise HikerError("HikerAPI returned a non-object JSON payload.")
        self.requests += 1
        save_json(cache_path, payload)
        time.sleep(random.uniform(0.4, 0.9))
        return payload

    @staticmethod
    def _request_with_retry(request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read(300).decode("utf-8", "replace")
                if exc.code in {401, 403}:
                    raise HikerError(
                        f"HikerAPI rejected the token (HTTP {exc.code}). Create a new token and update {TOKEN_ENV}."
                    ) from None
                if exc.code == 429:
                    last_error = exc
                    if attempt == 0:
                        time.sleep(5 + random.uniform(0, 3))
                        continue
                    raise HikerError("HikerAPI rate limit (HTTP 429). Wait and retry.") from None
                if exc.code >= 500 and attempt == 0:
                    last_error = exc
                    time.sleep(2 + random.uniform(0, 2))
                    continue
                raise HikerError(f"HikerAPI HTTP {exc.code}: {body}") from None
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise HikerError(f"HikerAPI network error: {exc}") from None
            except ValueError as exc:
                raise HikerError(f"HikerAPI returned invalid JSON: {exc}") from None
        raise HikerError(f"HikerAPI request failed: {last_error}")


def fetch_profile(client: HikerClient, username: str) -> dict[str, Any]:
    payload = client.get("/v2/user/by/username", {"username": username})
    user = payload.get("user")
    if not isinstance(user, dict):
        raise HikerError("HikerAPI response did not contain a user object.")
    return user


def _timeline_connection(payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    connection = (data or {}).get("xdt_api__v1__feed__user_timeline_graphql_connection")
    return connection if isinstance(connection, dict) else {}


def _next_cursor(payload: dict[str, Any], connection: dict[str, Any]) -> str | None:
    cursor = payload.get("next_page_id")
    if isinstance(cursor, str) and cursor:
        return cursor
    page_info = connection.get("page_info") or {}
    end_cursor = page_info.get("end_cursor")
    return end_cursor if isinstance(end_cursor, str) and end_cursor else None


def fetch_all_posts(client: HikerClient, username: str, limit: int) -> tuple[list[dict[str, Any]], bool, str]:
    """Return normalized posts, whether pagination ended, and a reason string."""
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    reason = "target_reached"
    while len(posts) < limit:
        payload = client.get("/g2/user/posts", {"username": username, "page_id": cursor})
        connection = _timeline_connection(payload)
        edges = connection.get("edges")
        if not isinstance(edges, list) or not edges:
            reason = "empty_page"
            break
        new_posts = 0
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            key = str(node.get("code") or node.get("pk") or node.get("id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            posts.append(InstagramSession._normalize_graphql_post(node))
            new_posts += 1
            if len(posts) >= limit:
                break
        if len(posts) >= limit:
            reason = "target_reached"
            break
        if new_posts == 0:
            reason = "pagination_returned_no_new_posts"
            break
        page_info = connection.get("page_info") or {}
        if not page_info.get("has_next_page"):
            reason = "source_exhausted"
            break
        cursor = _next_cursor(payload, connection)
        if not cursor:
            reason = "missing_cursor"
            break
        if cursor in seen_cursors:
            reason = "pagination_cursor_repeated"
            break
        seen_cursors.add(cursor)
    return posts, reason == "source_exhausted", reason


def caption_text(post: dict[str, Any]) -> str:
    caption = post.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "")
    return str(caption or "")


def extract_hashtags(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"#([0-9A-Za-z_]+)", text)))


def extract_mentions(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"@([0-9A-Za-z._]+)", text)))


def post_media(post: dict[str, Any]) -> list[dict[str, Any]]:
    children = post.get("carousel_media")
    if isinstance(children, list) and children:
        return [child for child in children if isinstance(child, dict)]
    return [post]


def collect_comments(client: HikerClient, post: dict[str, Any], username: str,
                     limit: int) -> tuple[list[dict[str, Any]], str]:
    media_id = str(post.get("pk") or post.get("id") or "")
    if not media_id:
        return [], "unavailable"
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    status = "available"
    while len(comments) < limit:
        payload = client.get(
            "/v2/media/comments",
            {"id": media_id, "page_id": cursor, "can_support_threading": "true"},
        )
        response = payload.get("response") or {}
        batch = response.get("comments") if isinstance(response, dict) else None
        if not isinstance(batch, list):
            status = "unavailable"
            break
        for parent in batch:
            if not isinstance(parent, dict):
                continue
            flattened = [(parent, None, None)]
            for child in parent.get("preview_child_comments") or []:
                if isinstance(child, dict):
                    flattened.append((child, parent, parent.get("text")))
            for item, owner_parent, parent_text in flattened:
                identity = str(item.get("pk") or item.get("id") or "")
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                created = item.get("created_at") or item.get("created_at_utc")
                comments.append({
                    "id": identity or None,
                    "username": user.get("username"),
                    "is_profile_owner": bool(user.get("username") and
                                             str(user.get("username")).casefold() == username.casefold()),
                    "text": item.get("text", ""),
                    "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat()
                    if isinstance(created, (int, float)) else None,
                    "is_reply": owner_parent is not None,
                    "parent_id": str(owner_parent.get("pk") or "") if owner_parent else None,
                    "parent_text": parent_text,
                    "like_count": item.get("comment_like_count", item.get("like_count")),
                    "reply_count": item.get("child_comment_count", 0),
                })
                if len(comments) >= limit:
                    break
            if len(comments) >= limit:
                break
        next_cursor = payload.get("next_page_id")
        if not isinstance(next_cursor, str) or not next_cursor or not response.get("has_more_comments"):
            break
        cursor = next_cursor
    count = post.get("comment_count")
    if isinstance(count, int) and count > len(comments):
        status = "limited" if comments else status
    return comments[:limit], status


def set_comment_status(post: dict[str, Any]) -> None:
    count = post.get("comment_count")
    comments = post.get("exported_comments") or []
    if post.get("commenting_disabled") or count == 0:
        post["comments_status"] = "available"
    elif isinstance(count, int) and len(comments) >= count:
        post["comments_status"] = "available"
    else:
        post["comments_status"] = "limited" if comments else "unavailable"


def collect_profile(client: HikerClient, username: str, *, limit: int = MAX_POSTS,
                    download: bool = True, comments: bool = False,
                    max_comments: int = DEFAULT_MAX_COMMENTS) -> dict[str, Any]:
    if not 1 <= limit <= MAX_POSTS:
        raise ValueError(f"Post limit must be between 1 and {MAX_POSTS}.")
    started = time.monotonic()
    profile = fetch_profile(client, username)
    posts, exhausted, reason = fetch_all_posts(client, username, limit)
    if not posts:
        raise RuntimeError("HikerAPI returned no public posts for this profile.")

    for post in posts:
        text = caption_text(post)
        post["hashtags"] = extract_hashtags(text)
        post["mentions"] = extract_mentions(text)
        user = post.get("user") if isinstance(post.get("user"), dict) else {}
        post["owner_username"] = user.get("username")
        location = post.get("location") if isinstance(post.get("location"), dict) else {}
        post["location"] = location.get("name")

    comments_capped = False
    if comments:
        for index, post in enumerate(posts, start=1):
            try:
                post["exported_comments"], post["comments_status"] = collect_comments(
                    client, post, username, max_comments
                )
            except HikerRequestCapReached:
                comments_capped = True
                post["exported_comments"], post["comments_status"] = [], "unavailable"
            except HikerError as exc:
                post["exported_comments"], post["comments_status"] = [], "unavailable"
                post["comments_error"] = str(exc)[:200]
            print(f"Collected comments {index}/{len(posts)}.", flush=True)
    else:
        for post in posts:
            post["exported_comments"] = []
            set_comment_status(post)

    folder = Path(os.getenv("IG_OUTPUT_DIR", "exports").strip() or "exports")
    if not folder.is_absolute():
        folder = ROOT / folder
    folder = folder / f"instagram-{username}-hiker-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    media_dir = folder / "media"

    records = build_post_records(posts, media_dir, download, True, max_workers=2)
    by_code = {record.get("shortcode"): record for record in records}
    result = [by_code[post.get("code")] for post in posts if post.get("code") in by_code]
    for source, record in zip(posts, result):
        media = post_media(source)
        record.update({
            "comment_count": source.get("comment_count"),
            "like_count": source.get("like_count"),
            "owner_username": source.get("owner_username"),
            "hashtags": source.get("hashtags") or [],
            "mentions": source.get("mentions") or [],
            "location": source.get("location"),
        })
        record["source_media_count"] = len(media)
        record["video_preview_only"] = any(
            item.get("is_video") and not item.get("video_versions") for item in media
        )

    advertised = profile.get("media_count")
    complete = (len(result) >= min(limit, advertised) if isinstance(advertised, int)
                else len(result) >= limit)
    comments_collected = sum(len(post.get("comments") or []) for post in result)
    files = [item for post in result for item in post.get("media_files", [])]
    account = {
        "username": profile.get("username") or username,
        "full_name": profile.get("full_name"),
        "biography": profile.get("biography"),
        "follower_count": profile.get("follower_count"),
        "following_count": profile.get("following_count"),
        "media_count": advertised,
        "is_verified": profile.get("is_verified"),
        "category": profile.get("category"),
    }
    coverage = {
        "requested_posts": limit,
        "advertised_posts": advertised,
        "collected_posts": len(result),
        "discovery_complete": complete,
        "pagination_reason": reason,
        "pages_exhausted": exhausted,
        "posts_with_dates": sum(bool(post.get("taken_at")) for post in result),
        "comments_collected": comments_collected,
        "comment_threads_complete": sum(post.get("comments_status") == "available" for post in result),
        "comment_threads_limited": sum(post.get("comments_status") == "limited" for post in result),
        "comment_threads_unavailable": sum(post.get("comments_status") == "unavailable" for post in result),
        "video_preview_only_posts": sum(post["video_preview_only"] for post in result),
        "media_files_saved": sum(bool(item.get("file")) for item in files),
        "media_download_errors": sum(bool(item.get("download_error")) for item in files),
        "media_bytes": sum(item.get("bytes", 0) for item in files),
    }
    warnings = []
    if not complete:
        warnings.append(
            f"Collected {len(result)} of {advertised if advertised is not None else 'unknown'} advertised posts "
            f"({reason})."
        )
    if comments_capped:
        warnings.append("HikerAPI request cap reached during comment collection; "
                        "remaining threads are marked unavailable.")
    export = {
        "account": account,
        "profile_url": f"https://www.instagram.com/{username}/",
        "provider": "HikerAPI",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "post_count": len(result),
        "posts": result,
        "comments_included": comments,
        "comments_mode": "full" if comments else "none",
        "coverage": coverage,
        "warnings": warnings,
        "provider_usage": {
            "provider": "HikerAPI",
            "base_url": client.base_url,
            "requests": client.requests,
            "cached_responses": client.cached,
            "request_cap": client.max_requests,
        },
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    save_json(folder / "posts.json", export)
    return export


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a public Instagram profile through HikerAPI (no Instagram cookies)."
    )
    parser.add_argument("profile", nargs="?", help="Instagram profile URL or username.")
    parser.add_argument("--limit", type=int, default=MAX_POSTS, metavar=f"1..{MAX_POSTS}",
                        help="Maximum number of posts to collect.")
    parser.add_argument("--no-download", action="store_true", help="Save metadata and captions only.")
    parser.add_argument("--comments", action="store_true", help="Collect comment threads (costs extra requests).")
    parser.add_argument("--max-comments", type=int, default=DEFAULT_MAX_COMMENTS,
                        help="Maximum comments per post.")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                        help="Hard cap on paid HikerAPI requests for this run.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached HikerAPI responses.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env()
    token = os.getenv(TOKEN_ENV, "").strip()
    profile_input = args.profile or input("Instagram profile URL or username: ").strip()
    username = profile_username_from_input(profile_input)
    cache_dir = ROOT / ".runtime" / "hiker" / username
    client = HikerClient(token, cache_dir, max_requests=args.max_requests, refresh=args.refresh)
    export = collect_profile(
        client,
        username,
        limit=args.limit,
        download=not args.no_download,
        comments=args.comments,
        max_comments=args.max_comments,
    )
    coverage = export["coverage"]
    print(
        f"Collected {coverage['collected_posts']} of {coverage['advertised_posts']} advertised posts; "
        f"{coverage['media_files_saved']} media files; {client.requests} HikerAPI requests, "
        f"{client.cached} cached.",
        flush=True,
    )
    if not coverage["discovery_complete"]:
        print(f"Coverage is incomplete: {coverage['pagination_reason']}.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
