"""Small, opt-in public Instagram fetch through Scrape.do (one request per run)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analyze_instagram_profile import load_dotenv
from scrape_instagram import ROOT, InstagramSession, post_to_record, profile_username_from_input


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The API token belongs to Scrape.do and must not follow a redirect.
        return None


class PageData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata = {}
        self.documents = []
        self.script_parts = None
        self.title_parts = None
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("content"):
            name = attrs.get("property") or attrs.get("name")
            if name in {"og:title", "og:description", "og:image", "description"}:
                self.metadata[name] = attrs["content"]
        if tag == "script" and attrs.get("type") in {"application/json", "application/ld+json"}:
            self.script_parts = []
        if tag == "title":
            self.title_parts = []

    def handle_data(self, value):
        if self.script_parts is not None:
            self.script_parts.append(value)
        if self.title_parts is not None:
            self.title_parts.append(value)

    def handle_endtag(self, tag):
        if tag == "title" and self.title_parts is not None:
            self.title = "".join(self.title_parts)
            self.title_parts = None
        if tag == "script" and self.script_parts is not None:
            try:
                self.documents.append(json.loads("".join(self.script_parts)))
            except json.JSONDecodeError:
                pass
            self.script_parts = None


def post_shortcode(value: dict) -> str | None:
    code = value.get("shortcode") or value.get("code")
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{5,30}", code):
        return code
    canonical = value.get("seo_canonical_url")
    if isinstance(canonical, str):
        url = urlparse(canonical)
        if url.hostname and url.hostname.casefold() not in {"instagram.com", "www.instagram.com"}:
            return None
        parts = url.path.strip("/").split("/")
        for index, part in enumerate(parts[:-1]):
            if part in {"p", "reel", "tv"} and re.fullmatch(r"[A-Za-z0-9_-]{5,30}", parts[index + 1]):
                return parts[index + 1]
    return None


def inspect_body(body: str, username: str) -> dict:
    parser = PageData()
    parser.feed(body)
    documents = list(parser.documents)
    try:
        documents.insert(0, json.loads(body))
    except json.JSONDecodeError:
        pass
    nodes = {}
    cursor = None
    has_next_page = False
    profile_found = False
    def walk(value, depth=0):
        nonlocal cursor, profile_found, has_next_page
        if depth > 60:
            return
        if isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, dict):
            if str(value.get("username") or "").casefold() == username.casefold():
                profile_found = True
            for name in ("xdt_api__v1__feed__user_timeline_graphql_connection", "edge_owner_to_timeline_media", "polaris_timeline_connection"):
                connection = value.get(name)
                if isinstance(connection, dict):
                    for edge in connection.get("edges", []):
                        node = edge.get("node") if isinstance(edge, dict) else None
                        if not isinstance(node, dict):
                            continue
                        code = post_shortcode(node)
                        identity = code or str(node.get("pk") or node.get("id") or "")
                        if identity and any(k in node for k in ("media_type", "display_url", "image_versions2")):
                            nodes[identity] = {**node, "shortcode": code}
                    info = connection.get("page_info") or {}
                    if info.get("has_next_page"):
                        has_next_page = True
                        cursor = info.get("end_cursor")
            for child in value.values():
                if isinstance(child, (list, dict)):
                    walk(child, depth + 1)
    for document in documents:
        walk(document)
    return {"title": parser.title[:200], "metadata": parser.metadata,
        "json_documents": len(documents), "profile_found": profile_found,
        "post_count": len(nodes), "post_nodes": list(nodes.values()), "has_next_page": has_next_page,
        "next_cursor": cursor, "login_form_present": 'name="username"' in body and 'name="password"' in body}


def fetch_profile(username: str, token: str, *, render: bool = False,
                  profile_json: bool = False) -> tuple[dict, str]:
    username = profile_username_from_input(username)
    target = ("https://www.instagram.com/api/v1/users/web_profile_info/?" + urlencode({"username": username})
              if profile_json else f"https://www.instagram.com/{username}/")
    return fetch_url(target, token, render=render)


def fetch_url(target: str, token: str, *, render: bool = False, options: dict | None = None,
              form: dict | None = None, target_headers: dict | None = None) -> tuple[dict, str]:
    parsed = urlparse(target)
    if parsed.scheme != "https" or parsed.hostname not in {"www.instagram.com", "instagram.com"} or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS Instagram targets are supported.")
    parameters = {"token": token, "url": target, "disableRetry": "true", "timeout": "45000"}
    if render:
        parameters.update({"render": "true", "customWait": "3000"})
    parameters.update(options or {})
    protected = {"setcookies", "customheaders", "forwardheaders", "cookie", "cookies"}
    if any(str(k).casefold() in protected for k in parameters) or any(str(k).casefold() in {"url", "token"} for k in (options or {})):
        raise ValueError("Probe options cannot forward account cookies or replace authentication/target.")
    headers = {"Accept": "*/*"}
    if target_headers:
        if any(k.casefold() not in {"x-ig-app-id", "x-fb-lsd", "referer", "content-type"} for k in target_headers):
            raise ValueError("Only public app/referer headers are supported.")
        parameters["extraHeaders"] = "true"
        headers.update({"Sd-" + k: v for k, v in target_headers.items()})
    data = urlencode(form).encode() if form is not None else None
    request = Request("https://api.scrape.do/?" + urlencode(parameters), data=data, headers=headers)
    started = time.monotonic()
    try:
        response = build_opener(NoRedirects()).open(request, timeout=75)
    except HTTPError as exc:
        response = exc
    except (URLError, OSError):
        raise RuntimeError("Scrape.do network request failed; credentials omitted from diagnostics.") from None
    with response:
        # Never print/save cookies, response headers wholesale, or the token-bearing API URL.
        body = response.read(20 * 1024 * 1024 + 1)
        if len(body) > 20 * 1024 * 1024:
            raise RuntimeError("Scrape.do response exceeded the 20 MB probe limit.")
        text = body.decode("utf-8", errors="replace")
        for secret in {token, quote(token, safe=""), quote_plus(token)}:
            text = text.replace(secret, "[redacted]")
        metadata = {"provider": "Scrape.do", "target": target, "render": render,
            "authenticated_instagram_session": False, "http_status": response.status,
            "duration_seconds": round(time.monotonic() - started, 3), "response_bytes": len(body),
            "content_type": response.headers.get("Content-Type"),
            "credits_used": response.headers.get("Scrape.do-Request-Cost"),
            "remaining_credits": response.headers.get("Scrape.do-Remaining-Credits")}
    return metadata, text


def export_sample(folder: Path, username: str, result: dict, *, download_sample: int = 0) -> dict:
    """Save compatible metadata and at most three single-media preview samples."""
    records = []
    sample_count = max(0, min(3, len(result["post_nodes"]), download_sample))
    for index, node in enumerate(result["post_nodes"], 1):
        post = InstagramSession._normalize_graphql_post(node)
        post["code"] = post.get("shortcode") or post.get("code")
        children = post.get("carousel_media")
        total_media = len(children) if isinstance(children, list) and children else 1
        if isinstance(children, list) and children:
            post["carousel_media"] = children[:1]
        record = post_to_record(post, index, folder / "media", index <= sample_count, False)
        record.setdefault("media_files", [])
        record.update({"source_media_count": total_media, "comments_status": "not_collected"})
        records.append(record)
    export = {"account": {"username": username}, "profile_url": f"https://www.instagram.com/{username}/",
        "provider": "Scrape.do", "experimental_probe": True, "exported_at": datetime.now(timezone.utc).isoformat(),
        "media_downloaded": False, "media_sample_post_count": sample_count, "comments_included": False,
        "post_count": len(records), "has_next_page": result["has_next_page"], "posts": records,
        "provider_request": {k: result.get(k) for k in ("http_status", "duration_seconds", "credits_used", "remaining_credits")}}
    (folder / "posts.json").write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return export


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", help="Public Instagram profile URL or username")
    parser.add_argument("--render", action="store_true", help="Use provider-side JavaScript rendering (higher credit cost)")
    parser.add_argument("--profile-json", action="store_true", help="Test the public profile JSON endpoint")
    parser.add_argument("--download-sample", type=int, choices=range(0, 4), default=0,
                        help="Download one preview media item for up to three posts (0–3)")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    token = os.environ.get("SCRAPER_API_KEY", "").strip()
    if not token:
        raise RuntimeError("Add SCRAPER_API_KEY to the local .env.")
    username = profile_username_from_input(args.profile)
    print(f"Scrape.do: one public request for @{username}; render={args.render}.", flush=True)
    metadata, body = fetch_profile(username, token, render=args.render, profile_json=args.profile_json)
    parsed = inspect_body(body, username)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = ROOT / ".runtime" / "scrapedo-probes" / f"{username}-{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "response.txt").write_text(body, encoding="utf-8")
    result = {**metadata, **parsed}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    usable = bool(result["post_count"] and result["profile_found"] and result["http_status"] == 200)
    if usable:
        export_sample(folder, username, result, download_sample=args.download_sample)
    print(json.dumps({k: v for k, v in result.items() if k not in {"post_nodes", "metadata", "next_cursor"}}, ensure_ascii=False), flush=True)
    print(f"Saved: {folder}", flush=True)
    return 0 if usable else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), flush=True)
        raise SystemExit(2)
