"""Analyze an exported Instagram profile with OpenCode Go's Muse Spark model.

The selected model is the Contributor variant: OpenCode documents that prompts
and completions may be used to train future Meta models. The CLI therefore
requires an explicit opt-in flag before it sends captions or media.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from analysis_prompts import (
    MAX_POSTS, PROMPT_VERSION, build_post_prompt, build_synthesis_prompt,
    comment_coverage, prepare_comments, DEFAULT_MAX_COMMENTS, DEFAULT_COMMENT_CHAR_BUDGET,
)

from media_processing import (
    IMAGE_JPEG_QUALITY,
    IMAGE_MAX_EDGE,
    VIDEO_CRF,
    VIDEO_MAX_EDGE,
    encode_image_jpeg,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "muse-spark-1.3-contributor"
DEFAULT_IMAGE_MAX_EDGE = 768
MAX_ANALYSIS_IMAGE_MAX_EDGE = 1280
DEFAULT_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
USER_AGENT = "instalyzer-profile-analyzer/1.0"
MAX_OUTPUT_TOKENS = 2048
SYNTHESIS_OUTPUT_TOKENS = 6500
DEFAULT_CONCURRENT_POST_ANALYSES = 6
MAX_CONCURRENT_POST_ANALYSES = 8
MAX_MEDIA_PER_POST = 6
ANALYSIS_PIPELINE_VERSION = PROMPT_VERSION
DEFAULT_POST_REASONING_EFFORT = "minimal"
DEFAULT_SYNTHESIS_REASONING_EFFORT = "low"
INPUT_COST_PER_MILLION_USD = 0.10
CACHED_INPUT_COST_PER_MILLION_USD = 0.002
OUTPUT_COST_PER_MILLION_USD = 0.20


class ModelResponseError(RuntimeError):
    """A failed model response that still carries its billable usage, when available."""

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting process variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


def setting_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"{name} має бути цілим числом; використовую {default}.", file=sys.stderr)
        return default
    if not minimum <= value <= maximum:
        print(f"{name} має бути в межах {minimum}–{maximum}; використовую {default}.", file=sys.stderr)
        return default
    return value


def setting_effort(name: str, default: str) -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        print(f"{name} має містити коректний reasoning effort; використовую {default}.", file=sys.stderr)
        return default
    return value


def select_media_samples(media_files: list, limit: int = MAX_MEDIA_PER_POST) -> list:
    """Choose evenly spaced media, retaining the first, middle, and last carousel items."""
    if len(media_files) <= limit:
        return list(media_files)
    if limit <= 1:
        return [media_files[0]]
    indexes = [round(i * (len(media_files) - 1) / (limit - 1)) for i in range(limit)]
    return [media_files[index] for index in indexes]


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def checkpoint_fingerprint(
    export_dir: Path,
    posts: list,
    account: str,
    model: str,
    post_effort: str,
    max_media: int,
    max_output_tokens: int,
    image_max_edge: int = DEFAULT_IMAGE_MAX_EDGE,
) -> str:
    source_posts = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        files = []
        for media in post.get("media_files", []):
            if not isinstance(media, dict) or not media.get("file"):
                continue
            path = resolve_media_path(export_dir, str(media["file"]))
            try:
                stat = path.stat()
                files.append((path.name, stat.st_size, stat.st_mtime_ns))
            except OSError:
                files.append((path.name, None, None))
        source_posts.append({
            "id": str(post.get("id") or post.get("shortcode") or ""),
            "caption": str(post.get("caption") or ""),
            "comments": post.get("comments", []),
            "comments_status": post.get("comments_status", "not_collected"),
            "media": files,
        })
    settings = {
        "pipeline_version": ANALYSIS_PIPELINE_VERSION,
        "account": account,
        "model": model,
        "post_reasoning_effort": post_effort,
        "max_media_per_post": max_media,
        "max_output_tokens": max_output_tokens,
        "posts": source_posts,
    }
    settings["image_max_edge"] = image_max_edge
    settings["comment_limit"] = os.getenv("OPENCODE_MAX_COMMENTS_PER_POST", str(DEFAULT_MAX_COMMENTS))
    settings["comment_char_budget"] = os.getenv("OPENCODE_COMMENT_CHAR_BUDGET", str(DEFAULT_COMMENT_CHAR_BUDGET))
    serialized = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def checkpoint_post_key(index: int, post: dict) -> str:
    identity = post.get("shortcode") or post.get("id") or f"post-{index}"
    return f"{index}:{identity}"


def load_checkpoint(path: Path, fingerprint: str, *, resume: bool) -> dict:
    empty = {"version": 1, "fingerprint": fingerprint, "posts": {}}
    if not resume or not path.is_file():
        return empty
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("Checkpoint пошкоджений; починаю аналіз із нуля.", file=sys.stderr)
        return empty
    if (
        not isinstance(saved, dict)
        or saved.get("version") != 1
        or saved.get("fingerprint") != fingerprint
        or not isinstance(saved.get("posts"), dict)
    ):
        print("Експорт або параметри змінилися; попередній checkpoint не підходить.")
        return empty
    return saved


def locate_export(argument: str | None) -> Path:
    if argument:
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if candidate.is_file() and candidate.name.lower() == "posts.json":
            return candidate.parent
        if candidate.is_dir() and (candidate / "posts.json").is_file():
            return candidate
        raise FileNotFoundError("Вказаний шлях не містить posts.json")

    exports_dir = ROOT / "exports"
    candidates = [p.parent for p in exports_dir.glob("instagram-*/posts.json")]
    if not candidates:
        raise FileNotFoundError("Не знайдено exports/instagram-*/posts.json")
    return max(candidates, key=lambda p: (p / "posts.json").stat().st_mtime)


def resolve_media_path(export_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    media_path = export_dir / "media" / path
    if media_path.exists():
        return media_path
    return export_dir / path


def ffmpeg_encode(path: Path, *, video: bool) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if video:
        command += ["-ss", "0"]
    command += [
        "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale=w='min({VIDEO_MAX_EDGE if video else IMAGE_MAX_EDGE},iw)':h=-2",
        # MJPEG qscale 6 is approximately quality 80 on FFmpeg's 2-31 scale.
        "-q:v", "6",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def image_data(path: Path, media_type: str) -> tuple[str, bytes] | None:
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        try:
            return "image/jpeg", encode_image_jpeg(
                path, max_edge=IMAGE_MAX_EDGE, quality=IMAGE_JPEG_QUALITY
            )
        except (OSError, ValueError):
            encoded = ffmpeg_encode(path, video=False)
            return ("image/jpeg", encoded) if encoded else None
    if suffix in {".mp4", ".mov", ".m4v", ".webm"} or "VIDEO" in media_type.upper():
        encoded = ffmpeg_encode(path, video=True)
        return ("image/jpeg", encoded) if encoded else None
    return None


def responses_text(payload: dict) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                value = part.get("text")
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())
    if not chunks:
        raise RuntimeError("API повернув відповідь без тексту")
    return "\n".join(chunks)


def response_usage(payload: dict, duration_seconds: float) -> dict[str, int | float | str]:
    """Normalize Responses API token usage and estimate Go cost using current model rates."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {"duration_seconds": round(duration_seconds, 3), "usage_available": 0}
    token_fields = (
        "input_tokens", "prompt_tokens", "inputTokens",
        "output_tokens", "completion_tokens", "outputTokens",
    )
    if not any(field in usage for field in token_fields):
        return {"duration_seconds": round(duration_seconds, 3), "usage_available": 0}

    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    if not isinstance(input_details, dict):
        input_details = {}
    if not isinstance(output_details, dict):
        output_details = {}

    if "input_tokens" in usage or "prompt_tokens" in usage:
        total_input = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        cached_input = int(
            input_details.get(
                "cached_tokens",
                usage.get("cache_read_tokens", usage.get("cached_tokens", 0)),
            ) or 0
        )
        billable_input = max(0, total_input - cached_input)
    else:
        # Support camelCase usage records where inputTokens is already uncached/billable.
        billable_input = int(usage.get("inputTokens", 0) or 0)
        cached_input = int(usage.get("cacheReadTokens", usage.get("cache_read_tokens", 0)) or 0)
        total_input = billable_input + cached_input

    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", usage.get("outputTokens", 0))) or 0)
    reasoning_tokens = int(output_details.get("reasoning_tokens", usage.get("reasoning_tokens", usage.get("reasoningTokens", 0))) or 0)
    estimated_cost = (
        billable_input * INPUT_COST_PER_MILLION_USD
        + cached_input * CACHED_INPUT_COST_PER_MILLION_USD
        + output_tokens * OUTPUT_COST_PER_MILLION_USD
    ) / 1_000_000
    reported_cost = payload.get("cost")
    if not isinstance(reported_cost, (int, float)):
        reported_cost = usage.get("cost_usd", usage.get("cost"))
    cost_source = "api" if isinstance(reported_cost, (int, float)) else "rate_estimate"
    cost = float(reported_cost) if cost_source == "api" else estimated_cost
    return {
        "input_tokens": billable_input,
        "uncached_input_tokens": billable_input,
        "cached_input_tokens": cached_input,
        "total_input_tokens": total_input,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cost_usd": round(cost, 10),
        "cost_source": cost_source,
        "duration_seconds": round(duration_seconds, 3),
        "usage_available": 1,
    }


def summarize_usage(usages: list[dict]) -> dict:
    available = [
        item for item in usages
        if isinstance(item, dict) and item.get("usage_available") == 1
    ]
    cost_sources = {str(item.get("cost_source")) for item in available}
    fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "total_input_tokens",
        "output_tokens",
        "reasoning_tokens",
    )
    totals = {field: sum(int(item.get(field, 0) or 0) for item in available) for field in fields}
    totals.update({
        "request_count": len(usages),
        "requests_with_usage": len(available),
        "requests_without_usage": len(usages) - len(available),
        "cost_usd": round(sum(float(item.get("cost_usd", 0) or 0) for item in available), 10),
        "cost_source": (
            "unavailable" if not cost_sources
            else next(iter(cost_sources)) if len(cost_sources) == 1
            else "mixed"
        ),
        "cost_is_complete": bool(available) and len(available) == len(usages),
        "request_duration_seconds_total": round(
            sum(float(item.get("duration_seconds", 0) or 0) for item in usages), 3
        ),
        "slowest_request_seconds": round(
            max((float(item.get("duration_seconds", 0) or 0) for item in usages), default=0), 3
        ),
        "pricing_usd_per_million_tokens": {
            "input": INPUT_COST_PER_MILLION_USD,
            "cached_input": CACHED_INPUT_COST_PER_MILLION_USD,
            "output": OUTPUT_COST_PER_MILLION_USD,
        },
    })
    return totals


def print_usage_summary(model_usage: dict) -> None:
    count = model_usage["request_count"]
    available = model_usage["requests_with_usage"]
    if not available:
        print(f"Usage недоступний: API не повернув лічильники для {count} запитів.")
        return

    cost_source = model_usage["cost_source"]
    if cost_source == "api":
        cost_label = "вартість за даними API"
    elif cost_source == "unavailable":
        cost_label = "вартість недоступна"
    elif cost_source == "mixed":
        cost_label = "змішана вартість (дані API + оцінка)"
    else:
        cost_label = "оцінка за тарифами OpenCode Go"
    completeness = "повна" if model_usage["cost_is_complete"] else "часткова"
    print(
        f"Usage ({available}/{count} запитів): "
        f"{model_usage['input_tokens']} uncached input + "
        f"{model_usage['cached_input_tokens']} cached input, "
        f"{model_usage['output_tokens']} output токенів; "
        f"{cost_label}: ${model_usage['cost_usd']:.6f} ({completeness})."
    )
    print(
        f"Модельні запити: сумарно {model_usage['request_duration_seconds_total']:.1f} с, "
        f"найдовший {model_usage['slowest_request_seconds']:.1f} с."
    )


def call_model(api_key: str, model: str, session_id: str, content: list[dict],
               max_output_tokens: int = MAX_OUTPUT_TOKENS,
               reasoning_effort: str = DEFAULT_POST_REASONING_EFFORT) -> tuple[str, dict]:
    started = time.monotonic()
    actual_effort = reasoning_effort
    effort_fallback = False

    def build_request(effort: str) -> Request:
        body = json.dumps(
            {"model": model, "input": [{"type": "message", "role": "user", "content": content}],
             "max_output_tokens": max_output_tokens, "reasoning": {"effort": effort}},
            ensure_ascii=False,
        ).encode("utf-8")
        return Request(
            f"{os.environ.get('OPENCODE_BASE_URL', DEFAULT_BASE_URL).rstrip('/')}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "x-opencode-session": session_id,
            },
            method="POST",
        )

    try:
        for attempt in range(4):
            try:
                with urlopen(build_request(actual_effort), timeout=180) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(30.0, max(1.0, float(retry_after))) if retry_after else 2.0 ** attempt
                    except ValueError:
                        delay = 2.0 ** attempt
                    exc.close()
                    time.sleep(delay)
                    continue
                if exc.code == 400 and actual_effort == "minimal" and attempt < 3:
                    error_text = exc.read().decode("utf-8", errors="replace").lower()
                    if "reasoning" in error_text and "effort" in error_text:
                        exc.close()
                        actual_effort = "low"
                        effort_fallback = True
                        continue
                raise
    except HTTPError as exc:
        # Do not print the response body: error payloads may echo request data.
        raise RuntimeError(f"OpenCode API повернув HTTP {exc.code}; тіло відповіді приховано") from None
    except URLError as exc:
        raise RuntimeError(f"Не вдалося з'єднатися з OpenCode API: {exc.reason}") from None
    except TimeoutError:
        raise RuntimeError("OpenCode API не відповів за 180 секунд") from None
    usage = response_usage(result, time.monotonic() - started)
    usage.update({
        "requested_reasoning_effort": reasoning_effort,
        "effective_reasoning_effort": actual_effort,
        "reasoning_effort_fallback": int(effort_fallback),
    })
    status = result.get("status")
    if status in {"failed", "cancelled", "incomplete"}:
        reason = (result.get("incomplete_details") or {}).get("reason")
        suffix = f" ({reason})" if reason else ""
        raise ModelResponseError(f"OpenCode API завершив запит зі статусом {status}{suffix}", usage)
    try:
        response = responses_text(result)
    except RuntimeError as exc:
        raise ModelResponseError(str(exc), usage) from None
    return response, usage


def analyze_post(api_key: str, model: str, session_id: str, export_dir: Path,
                 post: dict, index: int,
                 max_media_per_post: int, reasoning_effort: str,
                 max_output_tokens: int) -> dict:
    caption = post.get("caption") or ""
    shortcode = str(post.get("shortcode") or post.get("id") or f"post-{index}")
    permalink = post.get("permalink") or ""
    media_parts: list[dict] = []
    sent_media: list[str] = []
    media_payload_bytes = 0
    skipped_media = 0
    all_media = post.get("media_files", [])
    if not isinstance(all_media, list):
        all_media = []
    selected_media = select_media_samples(all_media, max_media_per_post)

    for media in selected_media:
        if not isinstance(media, dict) or not media.get("file"):
            skipped_media += 1
            continue
        path = resolve_media_path(export_dir, str(media["file"]))
        encoded = image_data(path, str(media.get("media_type") or ""))
        if not encoded:
            skipped_media += 1
            continue
        mime_type, binary = encoded
        b64 = base64.b64encode(binary).decode("ascii")
        media_parts.append({"type": "input_image", "image_url": f"data:{mime_type};base64,{b64}"})
        sent_media.append(path.name)
        media_payload_bytes += len(binary)

    comments = prepare_comments(
        post,
        setting_int("OPENCODE_MAX_COMMENTS_PER_POST", DEFAULT_MAX_COMMENTS, 1, 500),
        setting_int("OPENCODE_COMMENT_CHAR_BUDGET", DEFAULT_COMMENT_CHAR_BUDGET, 1000, 50000),
    )
    text = build_post_prompt(str(caption), comments, media_attached=len(media_parts),
        media_total=len(all_media), media_selected=len(selected_media), taken_at=post.get("taken_at"))
    content = [{"type": "input_text", "text": text}, *media_parts]
    analysis, usage = call_model(
        api_key, model, session_id, content,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
    )
    return {
        "shortcode": shortcode,
        "permalink": permalink,
        "caption": caption,
        "media_analyzed": sent_media,
        "media_payload_bytes": media_payload_bytes,
        "media_skipped": skipped_media,
        "media_total": len(all_media),
        "media_sampled_out": max(0, len(all_media) - len(selected_media)),
        "comments_status": comments["collection_status"],
        "comments_collected": comments["collected"],
        "comments_analyzed": comments["analyzed"],
        "comments_sampled_out": comments["sampled_out"],
        "comments_texts_truncated": comments["texts_truncated"],
        "owner_comment_evidence": [
            {"text": c["text"][:1000], "reply_to_text": (c.get("reply_to_text") or "")[:500]}
            for c in comments["comments"] if c["author_role"] == "profile_owner"
        ][:3],
        "analysis": analysis,
        "usage": usage,
    }


def synthesize_profile(api_key: str, model: str, session_id: str,
                       account: str, post_analyses: list[dict],
                       reasoning_effort: str = DEFAULT_SYNTHESIS_REASONING_EFFORT) -> tuple[str, dict]:
    text = build_synthesis_prompt(account, post_analyses)
    return call_model(
        api_key, model, session_id, [{"type": "input_text", "text": text}],
        max_output_tokens=SYNTHESIS_OUTPUT_TOKENS,
        reasoning_effort=reasoning_effort,
    )


def write_reports(export_dir: Path, output: dict) -> tuple[Path, Path]:
    account = str(output.get("account") or "profile")
    summary = str(output.get("profile_analysis") or "")
    results = output.get("posts") or []
    json_path = export_dir / "profile_analysis.json"
    markdown_path = export_dir / "profile_analysis.md"
    write_json_atomic(json_path, output)
    markdown = (
        f"# Підсумковий аналіз профілю @{account.lstrip('@')}\n\n"
        f"Висновки за фото, відеокадрами, підписами й доступними коментарями у {len(results)} публікаціях.\n\n"
        f"{summary.strip()}\n"
    )
    markdown_temp = markdown_path.with_name(markdown_path.name + ".tmp")
    markdown_temp.write_text(markdown, encoding="utf-8")
    markdown_temp.replace(markdown_path)
    return markdown_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Аналіз дописів і фото профілю Muse Spark 1.3 Contributor через OpenCode Go."
    )
    parser.add_argument("export", nargs="?", help="каталог експорту або шлях до posts.json")
    parser.add_argument(
        "--allow-contributor-training", action="store_true",
        help="підтвердити, що промпти/відповіді Contributor можуть використовуватися Meta для навчання",
    )
    parser.add_argument(
        "--synthesize-only", action="store_true",
        help="перегенерувати лише загальний підсумок із profile_analysis.json без повторного надсилання медіа",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="ігнорувати локальний checkpoint і повторно проаналізувати всі пости",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    global IMAGE_MAX_EDGE
    IMAGE_MAX_EDGE = setting_int(
        "OPENCODE_IMAGE_MAX_EDGE", DEFAULT_IMAGE_MAX_EDGE, 256, MAX_ANALYSIS_IMAGE_MAX_EDGE
    )
    max_workers = setting_int(
        "OPENCODE_MAX_CONCURRENT_POST_ANALYSES", DEFAULT_CONCURRENT_POST_ANALYSES, 1,
        MAX_CONCURRENT_POST_ANALYSES,
    )
    max_media_per_post = setting_int("OPENCODE_MAX_MEDIA_PER_POST", MAX_MEDIA_PER_POST, 1, 20)
    post_max_output_tokens = setting_int("OPENCODE_POST_MAX_OUTPUT_TOKENS", MAX_OUTPUT_TOKENS, 256, 4096)
    post_reasoning_effort = setting_effort(
        "OPENCODE_POST_REASONING_EFFORT", DEFAULT_POST_REASONING_EFFORT
    )
    synthesis_reasoning_effort = setting_effort(
        "OPENCODE_SYNTHESIS_REASONING_EFFORT", DEFAULT_SYNTHESIS_REASONING_EFFORT
    )
    if not args.allow_contributor_training:
        print(
            "Відправлення зупинено: Muse Spark 1.3 Contributor може використовувати промпти "
            "й відповіді для навчання майбутніх моделей Meta. Прочитай OpenCode Go Privacy "
            "і повтори запуск із --allow-contributor-training, якщо погоджуєшся.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not api_key:
        print("У локальному .env не задано OPENCODE_API_KEY. Не вставляй ключ у чат.", file=sys.stderr)
        return 2

    try:
        export_dir = locate_export(args.export)
        if args.synthesize_only:
            previous_path = export_dir / "profile_analysis.json"
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            results = previous.get("posts")
            if not isinstance(results, list) or not results:
                raise ValueError("У profile_analysis.json немає розборів постів")
            results = results[:MAX_POSTS]
            account = str(previous.get("account") or "profile")
            model = os.environ.get("OPENCODE_MODEL", str(previous.get("model") or DEFAULT_MODEL)).strip()
            session_id = str(uuid.uuid4())
            print(f"Оновлюю загальний підсумок для @{account.lstrip('@')} із {len(results)} розборів; медіа не надсилаю повторно.")
            analysis_started = time.monotonic()
            summary, summary_usage = synthesize_profile(
                api_key, model, session_id, account, results, synthesis_reasoning_effort
            )
            previous_usage = [item.get("usage", {}) for item in results]
            model_usage = summarize_usage([*previous_usage, summary_usage])
            synthesis_duration = round(time.monotonic() - analysis_started, 3)
            previous.update({
                "model": model,
                "provider": "OpenCode Go",
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "post_count": len(results),
                "posts": results,
                "profile_analysis": summary,
                "summary_usage": summary_usage,
                "model_usage": model_usage,
                "comment_coverage": comment_coverage(results),
                "analysis_settings": {
                    "post_reasoning_effort": post_reasoning_effort,
                    "synthesis_reasoning_effort": synthesis_reasoning_effort,
                    "max_media_per_post": max_media_per_post,
                    "post_max_output_tokens": post_max_output_tokens,
                    "image_max_edge": IMAGE_MAX_EDGE,
                },
                "analysis_duration_seconds": synthesis_duration,
                "synthesis_duration_seconds": summary_usage.get("duration_seconds", synthesis_duration),
            })
            markdown_path, json_path = write_reports(export_dir, previous)
            print(f"Збережено: {markdown_path}")
            print(f"Збережено: {json_path}")
            print_usage_summary(model_usage)
            return 0
        payload = json.loads((export_dir / "posts.json").read_text(encoding="utf-8"))
        posts = payload.get("posts", [])
        if not isinstance(posts, list) or not posts:
            raise ValueError("У posts.json немає списку дописів")
        if len(posts) > MAX_POSTS:
            print(f"Обмежую аналіз першими {MAX_POSTS} публікаціями з {len(posts)}.")
            posts = posts[:MAX_POSTS]
    except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Не вдалося прочитати експорт: {exc}", file=sys.stderr)
        return 2

    model = os.environ.get("OPENCODE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    raw_account = payload.get("account")
    if isinstance(raw_account, dict):
        raw_account = raw_account.get("username") or raw_account.get("name")
    account = str(raw_account or os.environ.get("IG_USERNAME") or "profile")
    session_id = str(uuid.uuid4())
    jobs = [(index, post) for index, post in enumerate(posts, 1) if isinstance(post, dict)]
    results_by_index: dict[int, dict] = {}
    run_fingerprint = checkpoint_fingerprint(
        export_dir, posts, account, model, post_reasoning_effort,
        max_media_per_post, post_max_output_tokens, IMAGE_MAX_EDGE,
    )
    checkpoint_path = export_dir / ".analysis_checkpoint.json"
    checkpoint = load_checkpoint(checkpoint_path, run_fingerprint, resume=not args.no_resume)
    saved_posts = checkpoint["posts"]
    pending_jobs = []
    for index, post in jobs:
        key = checkpoint_post_key(index, post)
        cached_result = saved_posts.get(key)
        if (
            isinstance(cached_result, dict)
            and isinstance(cached_result.get("analysis"), str)
            and isinstance(cached_result.get("usage"), dict)
        ):
            results_by_index[index] = cached_result
        else:
            pending_jobs.append((index, post))

    analysis_started = time.monotonic()
    worker_count = min(max_workers, max(1, len(pending_jobs)))
    print(
        f"Пости: {len(jobs)}; вже є в checkpoint: {len(results_by_index)}; "
        f"до API: {len(pending_jobs)}; паралельність: {worker_count}."
    )

    try:
        post_analysis_started = time.monotonic()
        first_failure: Exception | None = None
        with ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            future_indices = {
                executor.submit(
                    analyze_post, api_key, model, session_id, export_dir,
                    post, index, max_media_per_post, post_reasoning_effort,
                    post_max_output_tokens,
                ): (index, post)
                for index, post in pending_jobs
            }
            for future in as_completed(future_indices):
                index, post = future_indices[future]
                try:
                    result = future.result()
                except Exception as exc:
                    first_failure = first_failure or exc
                    print(f"Пост #{index} не збережено: {exc}", file=sys.stderr, flush=True)
                    continue
                results_by_index[index] = result
                checkpoint["posts"][checkpoint_post_key(index, post)] = result
                checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
                write_json_atomic(checkpoint_path, checkpoint)
                print(
                    f"Готово: {len(results_by_index)}/{len(jobs)}; "
                    f"пост #{index}; використано {len(result['media_analyzed'])}/"
                    f"{result['media_total']} медіафайлів.",
                    flush=True,
                )
        if first_failure is not None:
            raise RuntimeError(
                f"Один або кілька запитів завершилися помилкою. Успішні результати записані в "
                f"{checkpoint_path}; повторний запуск продовжить із них. Причина: {first_failure}"
            ) from None
        results = [results_by_index[index] for index, _ in jobs]
        if not results:
            raise ValueError("Не знайдено коректних постів для аналізу")
        post_analysis_duration = round(time.monotonic() - post_analysis_started, 3)
        summary, summary_usage = synthesize_profile(
            api_key, model, session_id, account, results, synthesis_reasoning_effort
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Аналіз зупинено: {exc}", file=sys.stderr)
        return 1

    model_usage = summarize_usage([item.get("usage", {}) for item in results] + [summary_usage])
    output = {
        "account": account,
        "model": model,
        "provider": "OpenCode Go",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "post_count": len(results),
        "profile_analysis": summary,
        "posts": results,
        "summary_usage": summary_usage,
        "model_usage": model_usage,
        "comment_coverage": comment_coverage(results),
        "analysis_settings": {
            "post_reasoning_effort": post_reasoning_effort,
            "synthesis_reasoning_effort": synthesis_reasoning_effort,
            "max_media_per_post": max_media_per_post,
            "post_max_output_tokens": post_max_output_tokens,
            "image_max_edge": IMAGE_MAX_EDGE,
            "max_concurrent_post_analyses": max_workers,
            "checkpoint_resumed_posts": len(jobs) - len(pending_jobs),
            "max_posts": MAX_POSTS,
            "prompt_version": ANALYSIS_PIPELINE_VERSION,
            "max_comments_per_post": setting_int("OPENCODE_MAX_COMMENTS_PER_POST", DEFAULT_MAX_COMMENTS, 1, 500),
            "comment_char_budget": setting_int("OPENCODE_COMMENT_CHAR_BUDGET", DEFAULT_COMMENT_CHAR_BUDGET, 1000, 50000),
        },
        "post_analysis_duration_seconds": post_analysis_duration,
        "synthesis_duration_seconds": summary_usage.get("duration_seconds", 0),
        "analysis_duration_seconds": round(time.monotonic() - analysis_started, 3),
    }
    markdown_path, json_path = write_reports(export_dir, output)
    print(f"Збережено: {markdown_path}")
    print(f"Збережено: {json_path}")
    print(f"Час аналізу: {output['analysis_duration_seconds']:.1f} с wall clock.")
    print(
        f"Етапи: аналіз постів {post_analysis_duration:.1f} с; "
        f"підсумок API {output['synthesis_duration_seconds']:.1f} с."
    )
    print_usage_summary(model_usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
