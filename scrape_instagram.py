#!/usr/bin/env python3
"""Export posts and media from the Instagram account configured in .env."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Semaphore
from typing import Any

from curl_cffi import requests as curl_requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from analysis_prompts import MAX_POSTS, DEFAULT_MAX_COMMENTS, comments_need_collection

from media_processing import (
    IMAGE_JPEG_QUALITY,
    IMAGE_MAX_EDGE,
    VIDEO_CRF,
    VIDEO_MAX_EDGE,
    compact_video_file,
    encode_image_jpeg,
)


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DEFAULT_MEDIA_DOWNLOAD_WORKERS = 4
MAX_MEDIA_DOWNLOAD_WORKERS = 6
MAX_CONCURRENT_VIDEO_TRANSCODES = 2
MAX_MEDIA_DOWNLOAD_ATTEMPTS = 2
VIDEO_TRANSCODE_SEMAPHORE = Semaphore(MAX_CONCURRENT_VIDEO_TRANSCODES)
RESERVED_PROFILE_PATHS = {
    "accounts", "about", "api", "developer", "direct", "explore", "legal",
    "p", "privacy", "reel", "reels", "stories", "web",
}
ALLOWED_MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
ALLOWED_MEDIA_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")
INSTAGRAM_API_BASE = "https://www.instagram.com/api/v1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def load_env() -> None:
    if not ENV_FILE.is_file():
        # Streamlit Cloud supplies credentials through environment variables.
        if os.getenv("IG_SESSIONID") and os.getenv("IG_USERNAME") and os.getenv("IG_DS_USER_ID"):
            return
        raise RuntimeError("Configure IG_USERNAME, IG_SESSIONID and IG_DS_USER_ID in .env or environment variables.")

    for raw_line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def profile_username_from_input(value: str) -> str:
    value = value.strip()
    if not value:
        raise RuntimeError("Enter an Instagram profile URL or username.")

    if value.startswith("@"):
        username = value[1:]
    elif "://" not in value and "/" not in value:
        username = value
    else:
        candidate = value if "://" in value else f"https://{value}"
        parsed = urllib.parse.urlparse(candidate)
        if (parsed.hostname or "").casefold() not in {"instagram.com", "www.instagram.com"}:
            raise RuntimeError("The link must point to a profile on instagram.com.")
        segments = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        if len(segments) != 1:
            raise RuntimeError("Provide a profile link such as https://www.instagram.com/username/.")
        username = segments[0].lstrip("@")

    if username.casefold() in RESERVED_PROFILE_PATHS or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        raise RuntimeError("That does not look like an Instagram profile username.")
    return username


class InstagramSession:
    """Use local cookies in a headless Chromium session and capture profile posts."""

    COOKIE_KEYS = (
        "IG_SESSIONID",
        "IG_CSRFTOKEN",
        "IG_DS_USER_ID",
        "IG_MID",
        "IG_IG_DID",
        "IG_RUR",
        "IG_DATR",
        "IG_DPR",
        "IG_IG_NRCB",
        "IG_PS_L",
        "IG_PS_N",
        "IG_RD_CHALLENGE",
        "IG_WD",
    )

    def __init__(self) -> None:
        cookies = [(key.removeprefix("IG_").lower(), os.getenv(key, "").strip()) for key in self.COOKIE_KEYS]
        if not cookies[0][1]:
            raise RuntimeError("Set IG_SESSIONID in .env. Keep the cookie value local; do not paste it into chat.")
        self.session = curl_requests.Session(impersonate="chrome")
        self.cookie_header = "; ".join(f"{name}={value}" for name, value in cookies if value)
        self.csrf_token = os.getenv("IG_CSRFTOKEN", "").strip()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.profile_url: str | None = None
        self.feed_response_count = 0
        self.posts_by_id: dict[str, dict[str, Any]] = {}
        self.has_next_page = False
        self.seen_cursors: set[str] = set()
        self.viewer_user: dict[str, Any] | None = None
        self.auth_error: str | None = None

    def prepare_profile(self, username: str) -> None:
        """Open the profile with its cookies in headless Chromium."""
        self.profile_url = f"https://www.instagram.com/{urllib.parse.quote(username, safe='._')}/"
        if not os.getenv("IG_SESSIONID", "").strip():
            raise RuntimeError("Set IG_SESSIONID in .env. Keep the cookie value local; do not paste it into chat.")
        cookie_objects = []
        for key in self.COOKIE_KEYS:
            value = os.getenv(key, "").strip()
            if not value:
                continue
            name = key.removeprefix("IG_").lower()
            cookie_objects.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                }
            )
        try:
            self.playwright = sync_playwright().start()
            executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or shutil.which("chromium") or shutil.which("chromium-browser")
            launch_options: dict[str, Any] = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if executable:
                launch_options["executable_path"] = executable
            self.browser = self.playwright.chromium.launch(**launch_options)
            self.context = self.browser.new_context(locale="en-US")
            self.context.add_cookies(cookie_objects)
            self.page = self.context.new_page()
            self.page.on("response", self._capture_feed_response)
            response = self.page.goto(self.profile_url, wait_until="domcontentloaded", timeout=60_000)
            if response is not None and response.status >= 400:
                raise RuntimeError(f"Instagram profile page returned HTTP {response.status}.")
        except PlaywrightError as exc:
            self.close()
            message = str(exc)
            if "Executable doesn't exist" in message or "browserType.launch" in message and "executable" in message.lower():
                raise RuntimeError("Playwright Chromium is missing. Run `python -m playwright install chromium`.") from None
            raise RuntimeError(f"Could not open the profile in Playwright: {type(exc).__name__}.") from None
        except RuntimeError:
            self.close()
            raise

    def _capture_feed_response(self, response: Any) -> None:
        if "/api/graphql" not in response.url:
            return
        try:
            body = response.text().lstrip()
            if body.startswith("for (;;);"):
                body = body[len("for (;;);") :]
            result = json.loads(body)
        except Exception:
            return
        if not isinstance(result, dict):
            return
        if result.get("errorSummary") in {"Log in to continue", "Not Logged In"}:
            self.auth_error = "Instagram says the Playwright context is logged out. Check the cookies imported from .env."
            return

        data = result.get("data")
        if not isinstance(data, dict):
            return
        viewer = data.get("xdt_viewer")
        if isinstance(viewer, dict) and isinstance(viewer.get("user"), dict):
            self.viewer_user = viewer["user"]

        connection = data.get("xdt_api__v1__feed__user_timeline_graphql_connection")
        if not isinstance(connection, dict):
            return
        self.feed_response_count += 1
        edges = connection.get("edges", [])
        if isinstance(edges, list):
            for edge in edges:
                if len(self.posts_by_id) >= MAX_POSTS:
                    break
                item = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_graphql_post(item)
                item_id = str(normalized.get("pk") or normalized.get("id") or "")
                if item_id:
                    self.posts_by_id.setdefault(item_id, normalized)

        page_info = connection.get("page_info")
        if isinstance(page_info, dict):
            self.has_next_page = (
                bool(page_info.get("has_next_page")) and len(self.posts_by_id) < MAX_POSTS
            )
            cursor = page_info.get("end_cursor")
            if cursor:
                cursor = str(cursor)
                if cursor in self.seen_cursors:
                    self.auth_error = "Instagram repeated a post cursor; pagination stopped to avoid duplicate data."
                else:
                    self.seen_cursors.add(cursor)

    def close(self) -> None:
        for resource in (self.context, self.browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self.context = None
        self.browser = None
        self.page = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    def get_json(self, endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {})
        url = f"{INSTAGRAM_API_BASE}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme != "https" or (parsed_url.hostname or "").casefold() != "www.instagram.com":
            raise RuntimeError("Refusing to send Instagram cookies to an unexpected host.")

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.instagram.com",
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": self.cookie_header,
        }
        if self.csrf_token:
            headers["X-CSRFToken"] = self.csrf_token
        try:
            response = self.session.get(url, headers=headers, timeout=45, allow_redirects=False)
        except Exception as exc:
            raise RuntimeError(f"Could not reach Instagram: {type(exc).__name__}.") from None

        response_content_type = response.headers.get("Content-Type", "")
        response_url = str(response.url)
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            destination = urllib.parse.urlparse(urllib.parse.urljoin(response_url, location))
            redirect_path = destination.path or "/"
            if (
                destination.scheme == "https"
                and (destination.hostname or "").casefold() == "www.instagram.com"
            ):
                raise RuntimeError(
                    "Instagram redirected the authenticated API request "
                    f"(HTTP {response.status_code}) to {redirect_path}; the session was not accepted."
                )
            raise RuntimeError(
                f"Instagram returned HTTP {response.status_code} with a redirect to an unexpected destination "
                f"(path: {redirect_path}); the redirect was not followed."
            )
        if response.status_code >= 400:
            message = self._response_message(response.content)
            if response.status_code in {401, 403}:
                raise RuntimeError(
                    "Instagram rejected the session (HTTP %d%s). Refresh the local cookies and try again."
                    % (response.status_code, f": {message}" if message else "")
                )
            raise RuntimeError(
                f"Instagram API returned HTTP {response.status_code}{f': {message}' if message else ''}."
            )

        try:
            result = response.json()
        except json.JSONDecodeError:
            final_path = urllib.parse.urlparse(response_url).path
            if "html" in response_content_type.casefold() or final_path.startswith(("/accounts/login", "/challenge", "/checkpoint")):
                raise RuntimeError(
                    "Instagram did not accept the configured session cookies and returned an HTML page. "
                    f"Response path: {final_path or '/'}. IG_SESSIONID must be current and match IG_DS_USER_ID."
                ) from None
            raise RuntimeError(
                "Instagram returned a non-JSON response "
                f"(content type: {response_content_type or 'unknown'}, endpoint: {final_path or '/'})."
            ) from None
        if not isinstance(result, dict):
            raise RuntimeError("Instagram returned an unexpected response.")
        if result.get("message") in {"login_required", "checkpoint_required", "challenge_required"}:
            raise RuntimeError(
                f"Instagram says {result['message']}; the current cookie set cannot authorize this request."
            )
        if result.get("status") == "fail":
            message = str(result.get("message") or "request failed")
            raise RuntimeError(f"Instagram rejected the request: {message}.")
        return result

    @staticmethod
    def _response_message(payload: bytes) -> str:
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        message = body.get("message") if isinstance(body, dict) else None
        return str(message)[:180] if message else ""

    def current_user(self) -> dict[str, str]:
        username = os.getenv("IG_USERNAME", "").strip().lstrip("@")
        user_id = os.getenv("IG_DS_USER_ID", "").strip()
        if not username:
            raise RuntimeError("Set IG_USERNAME in .env to your Instagram username.")
        if not user_id:
            raise RuntimeError("Set IG_DS_USER_ID in .env to the value of your ds_user_id cookie.")
        return {"username": username, "user_id": user_id}

    def all_posts(
        self,
        username: str,
        expected_viewer_username: str,
        expected_viewer_id: str,
    ) -> list[dict[str, Any]]:
        if self.page is None:
            raise RuntimeError("Open the Instagram profile before collecting posts.")

        deadline = time.monotonic() + 20
        while self.feed_response_count == 0 and time.monotonic() < deadline:
            self.page.wait_for_timeout(200)
        if self.auth_error:
            raise RuntimeError(self.auth_error)
        if self.feed_response_count == 0:
            raise RuntimeError(
                "Playwright did not capture Instagram's profile-post response. "
                "Check that the profile is available and the cookies are present in .env."
            )

        actual_username = str((self.viewer_user or {}).get("username") or "")
        actual_user_id = str((self.viewer_user or {}).get("pk") or "")
        if actual_username and actual_username.casefold() != expected_viewer_username.casefold():
            raise RuntimeError(
                f"The session is signed in as @{actual_username}, "
                f"but IG_USERNAME is @{expected_viewer_username}."
            )
        if actual_user_id and expected_viewer_id and actual_user_id != expected_viewer_id:
            raise RuntimeError("IG_DS_USER_ID does not match the signed-in Instagram account.")

        stalled_scrolls = 0
        while self.has_next_page and len(self.posts_by_id) < MAX_POSTS:
            previous_response_count = self.feed_response_count
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            deadline = time.monotonic() + 8
            while self.feed_response_count == previous_response_count and time.monotonic() < deadline:
                self.page.wait_for_timeout(200)
            if self.auth_error:
                raise RuntimeError(self.auth_error)
            if self.feed_response_count == previous_response_count:
                stalled_scrolls += 1
                if stalled_scrolls >= 3:
                    raise RuntimeError(
                        "Instagram still reports more posts, but Playwright could not load the next page. "
                        f"Collected {len(self.posts_by_id)} posts before pagination stalled."
                    )
                self.page.mouse.wheel(0, 1400)
                continue
            stalled_scrolls = 0

        return list(self.posts_by_id.values())[:MAX_POSTS]

    @staticmethod
    def _find_connection(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if isinstance(value.get("edges"), list) and isinstance(value.get("page_info"), dict):
                return value
            for nested in value.values():
                found = InstagramSession._find_connection(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = InstagramSession._find_connection(nested)
                if found:
                    return found
        return None

    @staticmethod
    def _normalize_graphql_post(item: dict[str, Any]) -> dict[str, Any]:
        post = dict(item)
        media_type = item.get("media_type")
        post["is_video"] = str(media_type).upper() == "VIDEO" or media_type == 2
        caption = item.get("caption")
        if isinstance(caption, list):
            post["caption"] = caption[0] if caption and isinstance(caption[0], dict) else {}
        carousel = item.get("carousel_media")
        if isinstance(carousel, list):
            post["carousel_media"] = [
                InstagramSession._normalize_graphql_post(child)
                for child in carousel
                if isinstance(child, dict)
            ]
        elif isinstance(carousel, dict):
            edges = carousel.get("edges")
            if isinstance(edges, list):
                post["carousel_media"] = [
                    InstagramSession._normalize_graphql_post(edge["node"])
                    for edge in edges
                    if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
                ]
        return post

    def get_comments_json(self, media_id: str, params: dict[str, str]) -> dict[str, Any]:
        endpoint = f"media/{urllib.parse.quote(media_id, safe='')}/comments/"
        if self.page is None:
            return self.get_json(endpoint, params)
        url = f"{INSTAGRAM_API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        response = self.page.evaluate("""async ({url, csrf}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 20000);
            try {
                const response = await fetch(url, {credentials: 'include', signal: controller.signal,
                    headers: {'X-IG-App-ID':'936619743392459', 'X-CSRFToken': csrf,
                              'X-Requested-With':'XMLHttpRequest'}});
                let payload = null;
                try { payload = await response.json(); } catch (_) {}
                return {status: response.status, payload};
            } finally { clearTimeout(timer); }
        }""", {"url": url, "csrf": self.csrf_token})
        if response.get("status") != 200:
            raise RuntimeError(f"Instagram comments returned HTTP {response.get('status')}.")
        payload = response.get("payload")
        if not isinstance(payload, dict) or payload.get("status") == "fail":
            raise RuntimeError("Instagram comments are unavailable for this post.")
        return payload

    def all_comments(self, media_id: str, profile_username: str = "",
                     max_comments: int = DEFAULT_MAX_COMMENTS) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        self.comments_limit_reached = False
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        pages = 0
        while True:
            pages += 1
            if pages > 20:
                self.comments_limit_reached = True
                break
            params = {
                "can_support_threading": "true",
                "permalink_enabled": "false",
                "is_carousel_bumped_post": "false",
            }
            if cursor:
                params["min_id"] = cursor
            result = self.get_comments_json(media_id, params)
            batch = result.get("comments", [])
            if not isinstance(batch, list):
                raise RuntimeError("Instagram returned an unexpected comments list.")
            flattened = []
            for parent in batch:
                if not isinstance(parent, dict):
                    continue
                flattened.append((parent, None, None))
                for children_key in ("preview_child_comments", "child_comments"):
                    children = parent.get(children_key)
                    if isinstance(children, list):
                        flattened.extend((child, str(parent.get("pk") or parent.get("id") or ""), parent.get("text", "")) for child in children if isinstance(child, dict))
            for position, (comment, parent_id, parent_text) in enumerate(flattened):
                if not isinstance(comment, dict):
                    continue
                comment_id = str(comment.get("pk") or comment.get("id") or "")
                if comment_id and comment_id in seen_ids:
                    continue
                if comment_id:
                    seen_ids.add(comment_id)
                user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
                created_at = comment.get("created_at")
                comments.append(
                    {
                        "id": comment_id or None,
                        "username": user.get("username"),
                        "is_profile_owner": bool(profile_username) and str(user.get("username") or "").casefold() == profile_username.casefold(),
                        "is_reply": parent_id is not None,
                        "parent_id": parent_id,
                        "parent_text": parent_text,
                        "text": comment.get("text", ""),
                        "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat()
                        if isinstance(created_at, (int, float))
                        else None,
                        "like_count": comment.get("comment_like_count", comment.get("like_count", 0)),
                        "reply_count": comment.get("child_comment_count", comment.get("reply_count", 0)),
                    }
                )
                if len(comments) >= max_comments:
                    self.comments_limit_reached = position < len(flattened) - 1 or bool(result.get("next_min_id"))
                    return comments

            next_cursor = result.get("next_min_id")
            if not next_cursor:
                break
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                self.comments_limit_reached = True
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return comments


def collect_post_comments(session: InstagramSession, posts: list[dict[str, Any]], username: str,
                          max_comments: int = DEFAULT_MAX_COMMENTS) -> None:
    """Comments are best effort: a restricted thread never discards the post export."""
    rate_limited = False
    for index, post in enumerate(posts, 1):
        post["exported_comments"] = []
        post.pop("comments_error", None)
        if rate_limited:
            post["comments_status"] = "rate_limited"
        else:
            try:
                media_id = str(post.get("pk") or post.get("id") or "")
                if not media_id:
                    raise RuntimeError("Missing media identifier.")
                post["exported_comments"] = session.all_comments(media_id, username, max_comments)
                post["comments_status"] = "limited" if getattr(session, "comments_limit_reached", False) else "available"
            except Exception as exc:
                rate_limited = "429" in str(exc)
                post["comments_status"] = "rate_limited" if rate_limited else "unavailable"
                post["comments_error"] = "Instagram rate limit" if rate_limited else "Comments could not be retrieved"
        print(f"Collected comments {index}/{len(posts)}.", flush=True)


class InstagramRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a request carrying cookies away from www.instagram.com."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlparse(newurl)
        if destination.scheme != "https" or (destination.hostname or "").casefold() != "www.instagram.com":
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Refusing to forward Instagram cookies to a different host",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "instagram-post"


def select_media_candidate(candidates: list[dict[str, Any]], max_edge: int) -> dict[str, Any] | None:
    sized = [
        item for item in candidates
        if int(item.get("width") or 0) > 0 and int(item.get("height") or 0) > 0
    ]
    if not sized:
        return candidates[0]
    within_limit = [
        item for item in sized
        if max(int(item["width"]), int(item["height"])) <= max_edge
    ]
    if within_limit:
        return max(within_limit, key=lambda item: int(item["width"]) * int(item["height"]))
    return min(sized, key=lambda item: int(item["width"]) * int(item["height"]))


def media_url_for(item: dict[str, Any]) -> tuple[str | None, str]:
    media_type = "VIDEO" if item.get("is_video") else "IMAGE"
    if item.get("is_video"):
        versions = item.get("video_versions") or []
        if isinstance(versions, list):
            candidates = [v for v in versions if isinstance(v, dict) and isinstance(v.get("url"), str)]
            if candidates:
                selected = select_media_candidate(candidates, VIDEO_MAX_EDGE)
                if selected:
                    return selected["url"], media_type

    image_versions = item.get("image_versions2")
    versions = image_versions.get("candidates", []) if isinstance(image_versions, dict) else []
    if isinstance(versions, list):
        candidates = [v for v in versions if isinstance(v, dict) and isinstance(v.get("url"), str)]
        if candidates:
            selected = select_media_candidate(candidates, IMAGE_MAX_EDGE)
            if selected:
                return selected["url"], "IMAGE"
    return None, media_type


def media_extension(url: str, content_type: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in ALLOWED_MEDIA_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower())
    if guessed == ".jpe":
        guessed = ".jpg"
    return guessed if guessed in ALLOWED_MEDIA_EXTENSIONS else ".bin"


def allowed_media_host(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and any(host.endswith(suffix) for suffix in ALLOWED_MEDIA_HOST_SUFFIXES)


def download_media(url: str, destination: Path) -> tuple[str, int, int, dict[str, Any] | None]:
    if not allowed_media_host(url):
        raise RuntimeError("Instagram returned an unexpected media host; download skipped.")

    request = urllib.request.Request(
        url,
        headers={"Accept": "image/*,video/*;q=0.9,*/*;q=0.1", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            if not allowed_media_host(response.geturl()):
                raise RuntimeError("Media download redirected to an unexpected host; download skipped.")
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            original_extension = media_extension(url, content_type)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".download.part")
            size = 0
            try:
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        size += len(chunk)
                is_image = (
                    content_type.split(";", 1)[0].strip().lower().startswith("image/")
                    or original_extension in {".jpg", ".jpeg", ".png", ".webp"}
                )
                if is_image:
                    try:
                        compressed = encode_image_jpeg(
                            temporary,
                            max_edge=IMAGE_MAX_EDGE,
                            quality=IMAGE_JPEG_QUALITY,
                        )
                    except (OSError, ValueError):
                        compressed = b""
                    if compressed and len(compressed) < size:
                        destination = destination.with_suffix(".jpg")
                        optimized = destination.with_name(destination.name + ".part")
                        try:
                            optimized.write_bytes(compressed)
                            optimized.replace(destination)
                        finally:
                            optimized.unlink(missing_ok=True)
                        temporary.unlink()
                        return destination.name, len(compressed), size, {
                            "method": "jpeg",
                            "quality": IMAGE_JPEG_QUALITY,
                            "max_edge": IMAGE_MAX_EDGE,
                        }

                destination = destination.with_suffix(original_extension)
                temporary.replace(destination)
                compress_video = os.getenv("IG_COMPRESS_VIDEOS", "true").strip().casefold() in {"1", "true", "yes", "on"}
                if not is_image and original_extension in {".mp4", ".mov", ".m4v"} and compress_video:
                    with VIDEO_TRANSCODE_SEMAPHORE:
                        compacted = compact_video_file(destination)
                    final_size = destination.stat().st_size
                    if compacted:
                        return destination.name, final_size, size, {
                            "method": "h264_crf",
                            "crf": VIDEO_CRF,
                            "max_edge": VIDEO_MAX_EDGE,
                        }
                    return destination.name, final_size, size, None
                return destination.name, size, size, None
            finally:
                if temporary.exists():
                    temporary.unlink()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Media download returned HTTP {exc.code}.") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download media: {exc.reason}") from None


def download_media_with_retry(
    url: str, destination: Path
) -> tuple[str, int, int, dict[str, Any] | None]:
    """Retry once for transient CDN/network failures; leave permanent errors alone."""
    for attempt in range(MAX_MEDIA_DOWNLOAD_ATTEMPTS):
        try:
            return download_media(url, destination)
        except RuntimeError as exc:
            message = str(exc).casefold()
            transient = (
                "timed out" in message
                or "temporary failure" in message
                or "connection reset" in message
                or "remote end closed" in message
                or any(f"http {status}" in message for status in (408, 425, 429, 500, 502, 503, 504))
            )
            if not transient or attempt + 1 == MAX_MEDIA_DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(1)
    raise RuntimeError("Media download attempts exhausted.")


def post_to_record(
    post: dict[str, Any], index: int, media_dir: Path, download: bool, include_comments: bool
) -> dict[str, Any]:
    post_id = str(post.get("pk") or post.get("id") or f"post-{index}")
    shortcode = str(post.get("code") or "")
    caption_data = post.get("caption")
    caption = caption_data.get("text", "") if isinstance(caption_data, dict) else ""
    taken_at = post.get("taken_at")
    timestamp = None
    if isinstance(taken_at, (int, float)):
        timestamp = datetime.fromtimestamp(taken_at, timezone.utc).isoformat()

    children = post.get("carousel_media")
    media_items = children if isinstance(children, list) and children else [post]
    downloaded: list[dict[str, Any]] = []
    if download:
        post_stem = safe_filename(shortcode or post_id)
        for item_index, item in enumerate(media_items, start=1):
            if not isinstance(item, dict):
                continue
            url, media_type = media_url_for(item)
            if not url:
                downloaded.append({"media_type": media_type, "download_error": "No media URL in Instagram response."})
                continue
            try:
                filename, size, original_size, compression = download_media_with_retry(
                    url, media_dir / f"{post_stem}-{item_index:02d}"
                )
                media_record = {"media_type": media_type, "file": filename, "bytes": size}
                if original_size > size:
                    media_record["original_bytes"] = original_size
                if compression:
                    media_record["compression"] = compression
                if media_type == "VIDEO":
                    media_record["selected_max_edge"] = VIDEO_MAX_EDGE
                downloaded.append(media_record)
            except (OSError, RuntimeError) as exc:
                downloaded.append({"media_type": media_type, "download_error": str(exc)})

    record: dict[str, Any] = {
        "id": post_id,
        "shortcode": shortcode or None,
        "caption": caption,
        "media_type": "CAROUSEL" if isinstance(children, list) and children else ("VIDEO" if post.get("is_video") else "IMAGE"),
        "taken_at": timestamp,
        "permalink": f"https://www.instagram.com/p/{shortcode}/" if shortcode else None,
    }
    if download:
        record["media_files"] = downloaded
    if include_comments:
        record["comments"] = post.get("exported_comments", [])
        record["comments_status"] = post.get("comments_status", "unavailable")
        if post.get("comments_error"):
            record["comments_error"] = post["comments_error"]
    return record


def build_post_records(
    posts: list[dict[str, Any]],
    media_dir: Path,
    download: bool,
    include_comments: bool,
    max_workers: int = DEFAULT_MEDIA_DOWNLOAD_WORKERS,
) -> list[dict[str, Any]]:
    """Process posts concurrently while preserving their original export order."""
    if not download or len(posts) < 2 or max_workers <= 1:
        return [
            post_to_record(post, index, media_dir, download, include_comments)
            for index, post in enumerate(posts, start=1)
        ]

    worker_count = min(MAX_MEDIA_DOWNLOAD_WORKERS, max(1, max_workers), len(posts))
    records: list[dict[str, Any] | None] = [None] * len(posts)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_indexes = {
            executor.submit(
                post_to_record, post, index, media_dir, download, include_comments
            ): index - 1
            for index, post in enumerate(posts, start=1)
        }
        completed = 0
        for future in as_completed(future_indexes):
            records[future_indexes[future]] = future.result()
            completed += 1
            if completed % 10 == 0 or completed == len(posts):
                print(f"Prepared {completed}/{len(posts)} posts.", flush=True)
    return [record for record in records if record is not None]


def media_download_worker_count() -> int:
    raw = os.getenv("IG_MEDIA_DOWNLOAD_WORKERS", str(DEFAULT_MEDIA_DOWNLOAD_WORKERS)).strip()
    try:
        value = int(raw)
    except ValueError:
        print(
            f"IG_MEDIA_DOWNLOAD_WORKERS must be 1–{MAX_MEDIA_DOWNLOAD_WORKERS}; "
            f"using {DEFAULT_MEDIA_DOWNLOAD_WORKERS}.",
            file=sys.stderr,
        )
        return DEFAULT_MEDIA_DOWNLOAD_WORKERS
    if not 1 <= value <= MAX_MEDIA_DOWNLOAD_WORKERS:
        print(
            f"IG_MEDIA_DOWNLOAD_WORKERS must be 1–{MAX_MEDIA_DOWNLOAD_WORKERS}; "
            f"using {DEFAULT_MEDIA_DOWNLOAD_WORKERS}.",
            file=sys.stderr,
        )
        return DEFAULT_MEDIA_DOWNLOAD_WORKERS
    return value


def comment_limit() -> int:
    try:
        return max(1, min(500, int(os.getenv("IG_MAX_COMMENTS_PER_POST", str(DEFAULT_MAX_COMMENTS)))))
    except ValueError:
        return DEFAULT_MAX_COMMENTS


def collect_export_comments(export_dir: Path, configured_username: str) -> None:
    """Backfill old exports without downloading media or discarding valid threads."""
    manifest = export_dir / "posts.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    posts = payload.get("posts")
    if not isinstance(posts, list) or not posts or not all(isinstance(p, dict) for p in posts):
        raise RuntimeError("The export has no valid posts list.")
    pending = [post for post in posts[:MAX_POSTS] if comments_need_collection(post)]
    if not pending:
        return
    account = payload.get("account")
    raw_username = account.get("username") if isinstance(account, dict) else account
    username = profile_username_from_input(str(raw_username or payload.get("profile_url") or ""))
    if any(not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", str(post.get("id") or "")) for post in pending):
        raise RuntimeError("The export is missing numeric Instagram post IDs; create a new export.")
    session = InstagramSession()
    try:
        viewer = session.current_user()
        if viewer["username"].casefold() != configured_username.casefold():
            raise RuntimeError("IG_USERNAME does not match the signed-in Instagram account.")
        session.prepare_profile(username)
        collect_post_comments(session, pending, username, comment_limit())
    finally:
        session.close()
    for post in pending:
        post["comments"] = post.pop("exported_comments", [])
    payload["comments_included"] = True
    payload["comments_updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = manifest.with_name(manifest.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    print(f"Updated comments for {len(pending)} exported posts.", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export posts, captions, and media from the Instagram account configured in .env."
    )
    parser.add_argument("profile", nargs="?", help="Instagram profile URL or username.")
    parser.add_argument("--no-download", action="store_true", help="Save post metadata and captions only.")
    parser.add_argument("--collect-comments", type=Path, metavar="EXPORT_DIR",
                        help="Collect missing comments in an existing export, preserving its media.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env()

    configured_username = os.getenv("IG_USERNAME", "").strip().lstrip("@")
    if not configured_username:
        raise RuntimeError("Set IG_USERNAME in .env to the username of your own Instagram account.")
    if args.collect_comments is not None:
        collect_export_comments(args.collect_comments.resolve(), configured_username)
        return 0
    profile_input = args.profile or input("Instagram profile URL or username: ").strip()
    requested_username = profile_username_from_input(profile_input)
    session = InstagramSession()
    account = session.current_user()
    if account["username"].casefold() != configured_username.casefold():
        raise RuntimeError("IG_USERNAME does not match the signed-in Instagram account.")

    session.prepare_profile(requested_username)
    include_comments = os.getenv("IG_INCLUDE_COMMENTS", "true").strip().casefold() in {"1", "true", "yes", "on"}
    try:
        posts = session.all_posts(requested_username, account["username"], account["user_id"])
        if include_comments:
            collect_post_comments(session, posts, requested_username, comment_limit())
    finally:
        session.close()
    output_dir = Path(os.getenv("IG_OUTPUT_DIR", "exports").strip() or "exports")
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    account_dir = output_dir / f"instagram-{safe_filename(requested_username)}-{stamp}"
    media_dir = account_dir / "media"
    download = not args.no_download
    download_workers = media_download_worker_count()
    if download:
        print(
            f"Media downloads: {download_workers} workers; video transcodes capped at "
            f"{MAX_CONCURRENT_VIDEO_TRANSCODES}.",
            flush=True,
        )
    records = build_post_records(
        posts, media_dir, download, include_comments, download_workers
    )

    result = {
        "account": {"username": requested_username},
        "profile_url": f"https://www.instagram.com/{requested_username}/",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "media_downloaded": not args.no_download,
        "comments_included": include_comments,
        "post_count": len(records),
        "posts": records,
    }
    account_dir.mkdir(parents=True, exist_ok=True)
    output_path = account_dir / "posts.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Exported {len(records)} posts for @{requested_username}.")
    if not args.no_download:
        print(f"Media files: {media_dir}")
    print(f"Post metadata and captions: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
