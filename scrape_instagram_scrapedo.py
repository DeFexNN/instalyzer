"""Cached public Instagram collection through Scrape.do, with a credit ledger."""
from __future__ import annotations

import hashlib
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from analyze_instagram_profile import load_dotenv
from scrape_instagram import ROOT, InstagramSession, build_post_records, post_to_record, profile_username_from_input
from scrapedo_probe import PageData, fetch_url, inspect_body, post_shortcode


class ScrapeDoClient:
    def __init__(self, token: str, folder: Path, *, max_credits: int = 10):
        self.token = token
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.max_credits = max_credits
        self.ledger_path = folder / "requests.json"
        self.ledger = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.is_file() else []
        self.cache_hits = 0
        self.new_requests = 0
        self.profile_metadata = {}

    @property
    def credits_used(self):
        return sum(float(row["credits_used"] if row.get("credits_used") is not None
                         else row.get("expected_credit_reserve", 1))
                   for row in self.ledger if not row.get("cached"))

    def cache_paths(self, target, render=False, options=None, form=None, target_headers=None):
        identity = json.dumps([target, render, options, form, target_headers], sort_keys=True)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
        return self.folder / f"{digest}.response.txt", self.folder / f"{digest}.metadata.json"

    def seed(self, metadata, body):
        """Reuse a previously saved public response without charging/logging it twice."""
        if metadata.get("authenticated_instagram_session") is not False:
            raise ValueError("Only explicitly anonymous responses can seed this cache.")
        body_path, info_path = self.cache_paths(metadata["target"], metadata.get("render", False))
        body_path.write_text(body, encoding="utf-8")
        info_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    def fetch(self, target: str, *, render=False, options=None, form=None, target_headers=None):
        body_path, info_path = self.cache_paths(target, render, options, form, target_headers)
        if body_path.is_file() and info_path.is_file():
            self.cache_hits += 1
            return json.loads(info_path.read_text(encoding="utf-8")), body_path.read_text(encoding="utf-8")
        residential = str((options or {}).get("super", "false")).lower() == "true"
        expected = (25 if render else 10) if residential else (5 if render else 1)
        if self.credits_used + expected > self.max_credits:
            raise RuntimeError("Configured Scrape.do credit budget reached.")
        self.new_requests += 1
        try:
            metadata, body = fetch_url(target, self.token, render=render, options=options, form=form, target_headers=target_headers)
        except RuntimeError:
            self.ledger.append({"target": target, "network_error": True, "credits_used": None,
                                "expected_credit_reserve": expected})
            self._save()
            raise
        body_path.write_text(body, encoding="utf-8")
        info_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata["expected_credit_reserve"] = expected
        self.ledger.append(metadata)
        self._save()
        print(f"Scrape.do request {len(self.ledger)}: HTTP {metadata['http_status']}; credits {metadata.get('credits_used')}; total {self.credits_used:g}.", flush=True)
        return metadata, body

    def _save(self):
        temporary = self.ledger_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.ledger_path)


def media_shortcode(node):
    code = post_shortcode(node)
    if code:
        return code
    # Instagram's public numeric media ID is a base-64 shortcode; no date is inferred.
    numeric = str(node.get("pk") or "").split("_")[0]
    if not numeric.isdigit() or not 0 < int(numeric) < 2**64:
        return None
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    value, code = int(numeric), ""
    while value:
        code = alphabet[value & 63] + code
        value >>= 6
    return code


def body_documents(body):
    """Read HTML, raw GraphQL JSON and Scrape.do's documented JSON envelope."""
    documents, seen = [], set()
    def decode(text, depth=0):
        if depth > 3 or text in seen:
            return
        seen.add(text)
        try:
            value = json.loads(text)
        except ValueError:
            page = PageData()
            page.feed(text)
            documents.extend(page.documents)
            return
        if isinstance(value, dict) and isinstance(value.get("content"), str) and "networkRequests" in value:
            decode(value["content"], depth + 1)
            for response in value.get("networkRequests") or []:
                if isinstance(response, dict) and isinstance(response.get("response_body"), str):
                    decode(response["response_body"], depth + 1)
        else:
            documents.append(value)
    decode(body)
    return documents


def walk_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, (dict, list)):
                yield from walk_objects(child)


def cached_details(client, username):
    """Enrich with already-paid post pages. Never fetch one page per post implicitly."""
    details = {}
    for path in client.folder.glob("*.metadata.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("http_status") != 200 or "/p/" not in meta.get("target", ""):
            continue
        response = path.with_name(path.name.replace(".metadata.json", ".response.txt"))
        if not response.is_file():
            continue
        for document in body_documents(response.read_text(encoding="utf-8")):
            for node in walk_objects(document):
                owner = node.get("user") or node.get("owner") or {}
                if (node.get("media_type") is not None and node.get("taken_at") is not None
                        and isinstance(owner, dict) and owner.get("username", "").casefold() == username.casefold()):
                    code = media_shortcode(node)
                    if code:
                        details[code] = node
    return details


def public_comments(node, username):
    raw = node.get("comments") or node.get("preview_comments") or []
    raw = list(raw) if isinstance(raw, list) else []
    connection = node.get("edge_media_to_parent_comment") or node.get("edge_media_to_comment") or {}
    raw += [edge.get("node", {}) for edge in connection.get("edges", []) if isinstance(edge, dict)]
    flattened = []
    for parent in raw:
        if not isinstance(parent, dict):
            continue
        flattened.append((parent, None, None))
        for name in ("preview_child_comments", "child_comments"):
            children = parent.get(name) or []
            if isinstance(children, list):
                flattened.extend((child, str(parent.get("pk") or parent.get("id") or ""), parent.get("text"))
                                 for child in children if isinstance(child, dict))
    comments = []
    seen = set()
    for item, parent_id, parent_text in flattened:
        owner = item.get("user") or item.get("owner") or {}
        owner = owner if isinstance(owner, dict) else {}
        text = item.get("text")
        identity = str(item.get("pk") or item.get("id") or "")
        if not text or (identity and identity in seen):
            continue
        seen.add(identity)
        author = str(owner.get("username") or "")
        created = item.get("created_at")
        comments.append({"id": identity or None, "text": text, "username": author,
                         "is_profile_owner": bool(author and author.casefold() == username.casefold()),
                         "is_reply": parent_id is not None, "parent_id": parent_id, "parent_text": parent_text,
                         "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat() if isinstance(created, (int, float)) else None,
                         "like_count": item.get("comment_like_count", item.get("like_count")),
                         "reply_count": item.get("child_comment_count", item.get("reply_count", 0))})
    count = node.get("comment_count", connection.get("count"))
    status = "available" if count == 0 or (isinstance(count, int) and len(comments) >= count) else ("partial" if comments else "unavailable")
    return comments[:100], "limited" if len(comments) > 100 else status


def normalized_post(node, username):
    post = InstagramSession._normalize_graphql_post(node)
    post["code"] = media_shortcode(node)
    post["exported_comments"], post["comments_status"] = public_comments(node, username)
    if post["comments_status"] == "unavailable":
        post["comments_error"] = "Public response did not include comment text; thread is not verified empty."
    return post


def discover_posts(client, username, limit):
    target = f"https://www.instagram.com/{username}/"
    metadata, body = client.fetch(target)
    if metadata["http_status"] != 200:
        raise RuntimeError(f"Public profile fetch returned HTTP {metadata['http_status']}.")
    initial = inspect_body(body, username)
    if not initial["post_count"] or not initial["profile_found"]:
        raise RuntimeError("Public response contains no verified profile timeline.")
    profile = next((n for d in body_documents(body) for n in walk_objects(d)
                    if str(n.get("username") or "").casefold() == username.casefold()
                    and "biography" in n), {})
    client.profile_metadata = {k: profile[k] for k in ("username", "full_name", "biography", "is_verified",
        "follower_count", "following_count", "category", "external_url") if k in profile}
    nodes = {str(n.get("pk") or n.get("id")): n for n in initial["post_nodes"]}
    advertised = None
    match = re.search(r"([\d,]+) Posts\b", initial["metadata"].get("og:description", ""))
    if match:
        advertised = int(match.group(1).replace(",", ""))
    query = next((n for d in body_documents(body) for n in walk_objects(d)
                  if n.get("queryName") == "PolarisProfilePostsLoggedOutTabGridUIContentQuery"), None)
    cursor = None
    seen_cursors = set()
    reason = "target_reached" if len(nodes) >= limit else "public_pagination_unavailable"
    while len(nodes) < limit and initial["has_next_page"] and query:
        variables = dict(query["variables"])
        variables["first"] = min(100, limit - len(nodes) if cursor else limit)
        if cursor:
            variables["after"] = cursor
        params = {"doc_id": query["queryID"], "variables": json.dumps(variables, separators=(",", ":"))}
        meta, response = client.fetch("https://www.instagram.com/graphql/query/?" + urlencode(params),
            target_headers={"X-IG-App-ID": "936619743392459", "Referer": target})
        if meta["http_status"] != 200:
            reason = f"pagination_http_{meta['http_status']}"
            break
        page = inspect_body(response, username)
        new = [n for n in page["post_nodes"] if str(n.get("pk") or n.get("id")) not in nodes]
        if not new:
            reason = "pagination_returned_no_new_posts"
            break
        nodes.update({str(n.get("pk") or n.get("id")): n for n in new})
        cursor = page["next_cursor"]
        if len(nodes) >= limit:
            reason = "target_reached"
            break
        if not page["has_next_page"]:
            reason = "source_exhausted"
            break
        if not cursor or cursor in seen_cursors:
            reason = "pagination_cursor_repeated"
            break
        seen_cursors.add(cursor)
        time.sleep(2)
    if not initial["has_next_page"]:
        reason = "source_exhausted"
    return list(nodes.values())[:limit], advertised, reason


def export_cached_posts(client, username, nodes, advertised, reason, *, limit=100, download=True):
    details = cached_details(client, username)
    posts = [normalized_post({**node, **details.get(media_shortcode(node), {})}, username) for node in nodes]
    # A completed export can be inspected/resumed without downloading its CDN files again.
    manifest = client.folder / "posts.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    previous_records = {p["shortcode"]: p for p in previous.get("posts", [])}
    records = []
    pending = []
    for post in posts:
        old = previous_records.get(post["code"])
        files = old.get("media_files", []) if old else []
        if download and files and all(f.get("file") and (client.folder / "media" / f["file"]).is_file() for f in files):
            fresh = post_to_record(post, 1, client.folder / "media", False, True)
            fresh["media_files"] = files
            records.append(fresh)
        else:
            pending.append(post)
    prepared = build_post_records(pending, client.folder / "media", download, True, max_workers=2)
    by_code = {p["shortcode"]: p for p in records + prepared}
    records = [by_code[post["code"]] for post in posts]
    for source, record in zip(posts, records):
        children = source.get("carousel_media") or [source]
        record["source_media_count"] = len(children)
        record["comments"] = source["exported_comments"]
        record["comments_status"] = source["comments_status"]
        record["taken_at"] = datetime.fromtimestamp(source["taken_at"], timezone.utc).isoformat() if isinstance(source.get("taken_at"), (int, float)) else None
        record["video_preview_only"] = bool(source.get("is_video") and not source.get("video_versions"))
        record["media_coverage"] = ("video_preview_only" if record["video_preview_only"] else
                                    "post_page" if record["shortcode"] in details else "public_grid_unverified")
        for field in ("like_count", "comment_count", "location", "accessibility_caption"):
            if field in source:
                record[field] = source[field]
    files = [f for p in records for f in p.get("media_files", [])]
    coverage = {"requested_posts": limit, "advertised_posts": advertised, "collected_posts": len(records),
        "discovery_complete": reason in {"source_exhausted", "target_reached"}, "stop_reason": reason,
        "posts_with_dates": sum(bool(p.get("taken_at")) for p in records),
        "comments_collected": sum(len(p["comments"]) for p in records),
        "verified_comment_threads": sum(p["comments_status"] == "available" for p in records),
        "video_preview_only_posts": sum(p["video_preview_only"] for p in records),
        "media_files_saved": sum(bool(f.get("file")) for f in files),
        "media_download_errors": sum(bool(f.get("download_error")) for f in files),
        "media_bytes": sum(f.get("bytes", 0) for f in files)}
    export = {"account": {**client.profile_metadata, "username": username}, "profile_url": f"https://www.instagram.com/{username}/",
        "provider": "Scrape.do", "experimental": True, "authenticated_instagram_session": False,
        "exported_at": datetime.now(timezone.utc).isoformat(), "post_count": len(records),
        "media_downloaded": download and not coverage["media_download_errors"],
        "comments_included": True, "coverage": coverage, "posts": records,
        "provider_usage": {"new_requests_this_run": client.new_requests, "cache_hits_this_run": client.cache_hits,
            "experiment_requests": len(client.ledger),
            "experiment_credits_reported": sum(float(r.get("credits_used") or 0) for r in client.ledger),
            "unknown_cost_requests": sum(r.get("credits_used") is None for r in client.ledger),
            "experiment_credit_budget_used": client.credits_used}}
    manifest.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    return export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=100, metavar="1..100")
    parser.add_argument("--max-credits", type=int, default=10, help="Total reported credit cap for this experiment folder")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed-response", type=Path, help="Reuse a saved scrapedo_probe response.txt and its result.json")
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    token = os.environ.get("SCRAPER_API_KEY", "").strip()
    if not token:
        raise RuntimeError("Add SCRAPER_API_KEY to the local .env.")
    username = profile_username_from_input(args.profile)
    folder = args.output or ROOT / ".runtime" / "scrapedo-100" / username
    client = ScrapeDoClient(token, folder, max_credits=args.max_credits)
    if args.seed_response:
        old = json.loads(args.seed_response.with_name("result.json").read_text(encoding="utf-8"))
        metadata = {k: old[k] for k in ("provider", "target", "render", "authenticated_instagram_session", "http_status", "duration_seconds", "response_bytes", "content_type", "credits_used", "remaining_credits")}
        client.seed(metadata, args.seed_response.read_text(encoding="utf-8"))
    started = time.monotonic()
    nodes, advertised, reason = discover_posts(client, username, args.limit)
    export = export_cached_posts(client, username, nodes, advertised, reason, limit=args.limit, download=not args.no_download)
    export["duration_seconds_this_run"] = round(time.monotonic() - started, 3)
    (folder / "posts.json").write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"coverage": export["coverage"], "provider_usage": export["provider_usage"], "duration_seconds_this_run": export["duration_seconds_this_run"]}, ensure_ascii=False), flush=True)
    print(f"Saved: {folder / 'posts.json'}", flush=True)
    return 0 if export["coverage"]["discovery_complete"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), flush=True)
        raise SystemExit(2)
