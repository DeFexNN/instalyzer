"""Collect public Instagram data with Apify's official Instagram Scraper."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analyze_instagram_profile import load_dotenv
from analysis_prompts import MAX_POSTS
from scrape_instagram import ROOT, build_post_records, post_to_record, profile_username_from_input

ACTOR_ID = "shu8hvrXbJbY3Eb9W"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApifyClient:
    def __init__(self, token: str, folder: Path, *, max_total_charge_usd=0.5):
        if not token:
            raise RuntimeError("Set APIFY_API_TOKEN in .env or Streamlit Secrets.")
        if not 0 < max_total_charge_usd <= 50:
            raise ValueError("APIFY_MAX_TOTAL_CHARGE_USD must be greater than 0 and at most 50.")
        self.token = token
        self.folder = folder / ".apify"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path = self.folder / "runs.json"
        self.runs = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.is_file() else {}
        self.budget = max_total_charge_usd
        self.new_runs = 0

    def request(self, path, *, method="GET", data=None, params=None):
        if not re.fullmatch(r"/[a-zA-Z0-9/_-]+", path):
            raise ValueError("Invalid Apify API path.")
        url = "https://api.apify.com/v2" + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                      method=method, headers={"Authorization": "Bearer " + self.token,
                      "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with build_opener(NoRedirects()).open(req, timeout=45) as response:
                body = response.read(30 * 1024 * 1024 + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            hint = " Check APIFY_API_TOKEN." if status in {401, 403} else ""
            raise RuntimeError(f"Apify HTTP {status}.{hint} No automatic paid-run retry.") from None
        except (URLError, OSError):
            raise RuntimeError("Apify network error. Existing run checkpoint retained; no automatic paid-run retry.") from None
        if len(body) > 30 * 1024 * 1024:
            raise RuntimeError("Apify response exceeds the 30 MB limit.")
        try:
            return json.loads(body.decode("utf-8").replace(self.token, "[redacted]"))
        except (ValueError, UnicodeError):
            raise RuntimeError("Apify returned invalid JSON.") from None

    @property
    def cost_usd(self):
        values = [row.get("usageTotalUsd") for row in self.runs.values()]
        settled = all(row.get("billing_settled") for row in self.runs.values())
        return round(sum(values), 8) if settled and all(isinstance(v, (int, float)) for v in values) else None

    def _checkpoint(self, key, run):
        kept = ("id", "status", "defaultDatasetId", "usageTotalUsd", "chargedEventCounts", "startedAt", "finishedAt", "buildNumber", "pricingInfo")
        self.runs[key].update({k: run[k] for k in kept if k in run})
        save_json(self.state_path, self.runs)

    def _settle_billing(self, key, result_count):
        # Live runs can report SUCCEEDED before all result charges reach usageTotalUsd.
        # Do not allocate a second run the apparently unused portion of that money.
        row = self.runs[key]
        if row.get("billing_settled"):
            return
        for attempt in range(6):
            info = row.get("pricingInfo") or {}
            events = (info.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
            price = (events.get("result") or {}).get("eventPriceUsd")
            billed = (row.get("chargedEventCounts") or {}).get("result", 0)
            cost = row.get("usageTotalUsd")
            if (set(events) == {"result"} and isinstance(price, (int, float)) and isinstance(cost, (int, float))
                    and billed >= result_count and cost + 1e-8 >= result_count * price):
                row["billing_settled"] = True
                save_json(self.state_path, self.runs)
                return
            if attempt == 5:
                break
            if attempt:
                time.sleep(2)
            self._checkpoint(key, self.request(f"/actor-runs/{row['id']}")["data"])

    def run(self, kind, actor_input):
        key = hashlib.sha256(json.dumps([ACTOR_ID, actor_input], sort_keys=True).encode()).hexdigest()[:24]
        items_path = self.folder / f"{key}.items.json"
        if key in self.runs and items_path.is_file():
            items = json.loads(items_path.read_text(encoding="utf-8"))
            self._settle_billing(key, len(items))
            return items
        row = self.runs.get(key)
        if row is None:
            spent = self.cost_usd
            if spent is None:
                raise RuntimeError("Apify cost is not yet known; refusing to start another paid run.")
            remaining = round(self.budget - spent, 8)
            if remaining < 0.0027:
                raise RuntimeError("Apify configured spending cap reached.")
            self.runs[key] = {"kind": kind, "status": "STARTING", "maxTotalChargeUsd": remaining}
            save_json(self.state_path, self.runs)
            # POST is deliberately never retried: a lost response may hide a live paid run.
            result = self.request(f"/acts/{ACTOR_ID}/runs", method="POST", data=actor_input,
                params={"build": "latest", "timeout": 600, "maxTotalChargeUsd": remaining,
                        "restartOnError": "false", "waitForFinish": 1})["data"]
            self.new_runs += 1
            self._checkpoint(key, result)
        elif not row.get("id"):
            raise RuntimeError("Apify start outcome is uncertain. Check Console before starting another run; checkpoint prevents duplicate charges.")
        started = time.monotonic()
        while self.runs[key].get("status") not in TERMINAL:
            if time.monotonic() - started > 660:
                self.abort_pending()
                raise RuntimeError("Apify run timeout; active run was sent an abort request.")
            print(f"Apify {kind}: {self.runs[key].get('status')}; waiting for saved run…", flush=True)
            result = self.request(f"/actor-runs/{self.runs[key]['id']}", params={"waitForFinish": 20})["data"]
            self._checkpoint(key, result)
            if result["status"] not in TERMINAL:
                time.sleep(2)
        row = self.runs[key]
        if row["status"] != "SUCCEEDED":
            raise RuntimeError(f"Apify {kind} run ended with {row['status']}; checkpoint retained.")
        dataset = row.get("defaultDatasetId")
        if not dataset:
            raise RuntimeError("Apify successful run has no dataset.")
        items = []
        while len(items) < 20000:
            batch = self.request(f"/datasets/{dataset}/items", params={"format": "json", "clean": "true", "offset": len(items), "limit": 500})
            if not isinstance(batch, list):
                raise RuntimeError("Apify returned an invalid dataset.")
            items.extend(batch)
            if len(batch) < 500:
                break
        save_json(items_path, items)
        self._settle_billing(key, len(items))
        print(f"Apify {kind}: {len(items)} results; reported total ${self.cost_usd if self.cost_usd is not None else 'unknown'}.", flush=True)
        return items

    def abort_pending(self):
        for key, row in self.runs.items():
            if row.get("id") and row.get("status") not in TERMINAL:
                result = self.request(f"/actor-runs/{row['id']}/abort", method="POST", data={})["data"]
                self._checkpoint(key, result)


def abort_export_runs(exports_root, token):
    if not token:
        return
    for state in Path(exports_root).glob("*/.apify/runs.json"):
        ApifyClient(token, state.parent.parent).abort_pending()


def timestamp(value):
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def shortcode(value):
    if not isinstance(value, str):
        return None
    found = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)/", value + "/")
    return found.group(1) if found else None


def normalize_comments(items, username, limit=100):
    comments, seen = [], set()
    def append(item, parent=None, depth=0):
        if not isinstance(item, dict) or depth > 5:
            return
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        owner = str(item.get("ownerUsername") or item.get("username") or "")
        identity = str(item.get("id") or "")
        dedup = identity or json.dumps([owner, text, item.get("timestamp")])
        if dedup in seen:
            for reply in item.get("replies") or []:
                append(reply, item, depth + 1)
            return
        seen.add(dedup)
        date = timestamp(item.get("timestamp"))
        parent_id = str(parent.get("id") or "") if parent else item.get("parentCommentId")
        comments.append({"id": identity or None, "username": owner or None,
            "is_profile_owner": bool(owner and owner.casefold() == username.casefold()),
            "text": text, "created_at": date.isoformat() if date else None,
            "is_reply": bool(parent_id), "parent_id": parent_id or None,
            "parent_text": parent.get("text") if parent else None,
            "like_count": item.get("likesCount"), "reply_count": item.get("repliesCount", 0)})
        for reply in item.get("replies") or []:
            append(reply, item, depth + 1)
    for item in items:
        append(item)
    by_id = {c["id"]: c for c in comments if c["id"]}
    for c in comments:
        if not c["parent_text"] and c["parent_id"] in by_id:
            c["parent_text"] = by_id[c["parent_id"]]["text"]
    return comments[:limit]


def media_node(item):
    video = str(item.get("type", "")).casefold() == "video" or bool(item.get("videoUrl"))
    image = item.get("displayUrl")
    node = {"pk": str(item.get("id") or ""), "is_video": video, "media_type": 2 if video else 1,
        "image_versions2": {"candidates": [{"url": image, "width": item.get("dimensionsWidth") or 1280,
                    "height": item.get("dimensionsHeight") or 1280}] if image else []}}
    if item.get("videoUrl"):
        node["video_versions"] = [{"url": item["videoUrl"], "width": item.get("dimensionsWidth") or 720,
                                   "height": item.get("dimensionsHeight") or 720}]
    return node


def set_comment_status(post):
    count = post.get("comment_count")
    comments = post.get("exported_comments") or []
    post["comments_status"] = ("available" if post.get("is_comments_disabled") or count == 0
        or (isinstance(count, int) and count >= 0 and len(comments) >= count)
        else "limited" if comments else "unavailable")


def merge_comments(existing, added, limit=100):
    combined, seen = [], {}
    for comment in existing + added:
        key = comment.get("id") or (comment.get("username"), comment.get("text"), comment.get("created_at"))
        if key in seen:
            saved = combined[seen[key]]
            for field, value in comment.items():
                if not saved.get(field) and value is not None:
                    saved[field] = value
        else:
            seen[key] = len(combined)
            combined.append(dict(comment))
    return combined[:limit]


def normalize_post(item, username):
    code = item.get("shortCode") or shortcode(item.get("url"))
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,30}", code):
        return None
    # A collaborative post may be owned by another account; the profile URL is the source.
    node = media_node(item)
    date = timestamp(item.get("timestamp"))
    node.update({"code": code, "caption": {"text": item.get("caption") or ""},
        "taken_at": date.timestamp() if date else None, "comment_count": item.get("commentsCount"),
        "like_count": item.get("likesCount"), "is_comments_disabled": item.get("isCommentsDisabled", False),
        "exported_comments": normalize_comments(item.get("latestComments") or [], username),
        "owner_username": item.get("ownerUsername"), "hashtags": item.get("hashtags") or [],
        "mentions": item.get("mentions") or [], "location": item.get("locationName"),
        "source_type": item.get("type")})
    children = [media_node(child) for child in item.get("childPosts") or [] if isinstance(child, dict)]
    if not children and str(item.get("type", "")).casefold() == "sidecar":
        children = [media_node({"displayUrl": url}) for url in item.get("images") or [] if isinstance(url, str)]
    if children:
        node["carousel_media"] = children
        node["media_type"] = 8
    set_comment_status(node)
    return node


def collect_extra_comments(client, posts, username, limit=100):
    needed = [p for p in posts if p.get("comments_status") != "available"]
    if not needed:
        return
    items = client.run("comments", {"directUrls": [f"https://www.instagram.com/p/{p['code']}/" for p in needed],
        "resultsType": "comments", "resultsLimit": limit})
    by_code = {p["code"]: p for p in needed}
    by_id = {p["pk"]: p for p in needed}
    grouped = {p["code"]: [] for p in needed}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = next((shortcode(item.get(k)) for k in ("postUrl", "inputUrl", "url") if shortcode(item.get(k))), None)
        target = by_code.get(code) or by_id.get(str(item.get("postId") or ""))
        if target is not None and item.get("text"):
            grouped[target["code"]].append(item)
    for p in needed:
        added = normalize_comments(grouped[p["code"]], username, limit)
        existing = p.get("exported_comments") or []
        p["exported_comments"] = merge_comments(existing, added, limit)
        set_comment_status(p)
    print(f"Collected comments {len(needed)}/{len(needed)}.", flush=True)


def provider_usage(client):
    return {"provider": "Apify", "actor_id": ACTOR_ID, "cost_usd": client.cost_usd,
            "cost_is_complete": client.cost_usd is not None, "actor_runs": len(client.runs),
            "new_runs_this_execution": client.new_runs}


def collect_profile(client, username, *, limit=100, comments_mode="preview", download=True):
    if not 1 <= limit <= MAX_POSTS:
        raise ValueError("Post limit must be between 1 and 100.")
    started = time.monotonic()
    items = client.run("posts", {"directUrls": [f"https://www.instagram.com/{username}/"],
        "resultsType": "posts", "resultsLimit": limit, "addParentData": True})
    unique = {}
    for item in items:
        if isinstance(item, dict) and not item.get("error"):
            post = normalize_post(item, username)
            if post:
                unique.setdefault(post["code"], post)
    posts = sorted(unique.values(), key=lambda p: p.get("taken_at") or 0, reverse=True)[:limit]
    if not posts:
        raise RuntimeError("Apify returned no valid public posts for this profile; dataset is checkpointed.")
    folder = client.folder.parent
    old = json.loads((folder / "posts.json").read_text(encoding="utf-8")) if (folder / "posts.json").is_file() else {}
    old_posts = {p.get("shortcode"): p for p in old.get("posts", [])}
    for p in posts:
        p["exported_comments"] = merge_comments(p["exported_comments"], old_posts.get(p["code"], {}).get("comments") or [])
        set_comment_status(p)
    account, warnings = {"username": username}, []
    try:
        profiles = client.run("profile", {"directUrls": [f"https://www.instagram.com/{username}/"],
                                        "resultsType": "details", "resultsLimit": 1})
        profile = next((p for p in profiles if isinstance(p, dict) and str(p.get("username") or "").casefold() == username.casefold()), {})
        mapping = {"fullName": "full_name", "biography": "biography", "followersCount": "follower_count",
                   "followsCount": "following_count", "postsCount": "media_count", "verified": "is_verified"}
        account.update({target: profile[source] for source, target in mapping.items() if source in profile})
    except RuntimeError as exc:
        warnings.append(str(exc))
    if comments_mode == "full":
        try:
            collect_extra_comments(client, posts, username)
        except RuntimeError as exc:
            warnings.append(str(exc))
    records, pending = {}, []
    for post in posts:
        previous = old_posts.get(post["code"], {})
        files = previous.get("media_files") or []
        media = post.get("carousel_media") or [post]
        upgrade_video = previous.get("video_preview_only") and any(m.get("video_versions") for m in media)
        if download and len(files) == len(media) and not upgrade_video and all(f.get("file") and (folder / "media" / f["file"]).is_file() for f in files):
            record = post_to_record(post, 1, folder / "media", False, True)
            record["media_files"] = files
            records[post["code"]] = record
        else:
            pending.append(post)
    prepared = build_post_records(pending, folder / "media", download, True, max_workers=2)
    records.update({p["shortcode"]: p for p in prepared})
    result = [records[p["code"]] for p in posts]
    for source, record in zip(posts, result):
        media = source.get("carousel_media") or [source]
        record.update({k: source[k] for k in ("comment_count", "like_count", "owner_username", "hashtags", "mentions", "location")})
        record["source_media_count"] = len(media)
        record["video_preview_only"] = any(m.get("is_video") and not m.get("video_versions") for m in media)
    files = [f for p in result for f in p.get("media_files", [])]
    advertised = account.get("media_count")
    coverage = {"requested_posts": limit, "advertised_posts": advertised, "collected_posts": len(result),
        "discovery_complete": len(result) >= min(limit, advertised) if isinstance(advertised, int) else len(result) >= limit,
        "posts_with_dates": sum(bool(p.get("taken_at")) for p in result),
        "comments_collected": sum(len(p["comments"]) for p in result),
        "comment_threads_complete": sum(p["comments_status"] == "available" for p in result),
        "comment_threads_limited": sum(p["comments_status"] == "limited" for p in result),
        "comment_threads_unavailable": sum(p["comments_status"] == "unavailable" for p in result),
        "video_preview_only_posts": sum(p["video_preview_only"] for p in result),
        "media_files_saved": sum(bool(f.get("file")) for f in files),
        "media_download_errors": sum(bool(f.get("download_error")) for f in files),
        "media_bytes": sum(f.get("bytes", 0) for f in files)}
    export = {"account": account, "profile_url": f"https://www.instagram.com/{username}/", "provider": "Apify",
        "exported_at": datetime.now(timezone.utc).isoformat(), "post_count": len(result), "posts": result,
        "comments_included": True, "comments_mode": comments_mode, "coverage": coverage, "warnings": warnings,
        "provider_usage": provider_usage(client), "duration_seconds": round(time.monotonic() - started, 3)}
    save_json(folder / "posts.json", export)
    return export


def backfill_comments(client, username):
    path = client.folder.parent / "posts.json"
    export = json.loads(path.read_text(encoding="utf-8"))
    if export.get("provider") != "Apify":
        raise RuntimeError("Apify comment resume requires an Apify export.")
    posts = [{"pk": p["id"], "code": p["shortcode"], "comment_count": p.get("comment_count"),
              "exported_comments": p.get("comments") or [], "comments_status": p.get("comments_status")} for p in export["posts"]]
    collect_extra_comments(client, posts, username)
    for source, record in zip(posts, export["posts"]):
        record["comments"], record["comments_status"] = source["exported_comments"], source["comments_status"]
    export["provider_usage"] = provider_usage(client)
    export["comments_included"] = True
    export["comments_mode"] = "full"
    coverage = export.setdefault("coverage", {})
    coverage.update({"comments_collected": sum(len(p["comments"]) for p in export["posts"]),
        "comment_threads_complete": sum(p["comments_status"] == "available" for p in export["posts"]),
        "comment_threads_limited": sum(p["comments_status"] == "limited" for p in export["posts"]),
        "comment_threads_unavailable": sum(p["comments_status"] == "unavailable" for p in export["posts"])})
    save_json(path, export)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?")
    parser.add_argument("--limit", type=int, choices=range(1, MAX_POSTS + 1), default=MAX_POSTS, metavar="1..100")
    parser.add_argument("--comments-mode", choices=("preview", "full"), default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--collect-comments", type=Path)
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if args.collect_comments:
        saved = json.loads((args.collect_comments / "posts.json").read_text(encoding="utf-8"))
        username = profile_username_from_input(saved["account"]["username"])
    elif args.profile:
        username = profile_username_from_input(args.profile)
    else:
        parser.error("Provide a profile or --collect-comments EXPORT_DIR")
    folder = args.collect_comments or args.output or Path(os.environ.get("IG_OUTPUT_DIR", str(ROOT / "exports"))) / f"instagram-{username}-apify"
    budget = args.max_cost_usd if args.max_cost_usd is not None else float(os.environ.get("APIFY_MAX_TOTAL_CHARGE_USD", "0.5"))
    client = ApifyClient(os.environ.get("APIFY_API_TOKEN", "").strip(), folder, max_total_charge_usd=budget)
    try:
        if args.collect_comments:
            backfill_comments(client, username)
        else:
            # Read after .env, while keeping an explicit CLI option authoritative.
            mode = args.comments_mode or os.environ.get("APIFY_COMMENTS_MODE", "preview")
            if mode not in {"preview", "full"}:
                raise ValueError("APIFY_COMMENTS_MODE must be preview or full.")
            export = collect_profile(client, username, limit=args.limit, comments_mode=mode, download=not args.no_download)
            print(json.dumps({"coverage": export["coverage"], "provider_usage": export["provider_usage"], "duration_seconds": export["duration_seconds"]}, ensure_ascii=False), flush=True)
        print(f"Saved: {folder / 'posts.json'}", flush=True)
        return 0
    except KeyboardInterrupt:
        try:
            client.abort_pending()
        except RuntimeError:
            pass
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), flush=True)
        raise SystemExit(2)
