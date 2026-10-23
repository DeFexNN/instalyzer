#!/usr/bin/env python3
"""Compare image-size and carousel-sampling settings on a small fixed sample.

Only aggregate timings, usage, payload sizes, and output lengths are saved. Captions,
images, post identifiers, and generated per-post analyses are not written to the report.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

import analyze_instagram_profile as analyzer


ROOT = Path(__file__).resolve().parent
SCENARIOS = [
    {"name": "baseline_1280px_3media_1024tokens", "image_max_edge": 1280, "max_media": 3, "max_output_tokens": 1024},
    {"name": "768px_only", "image_max_edge": 768, "max_media": 3, "max_output_tokens": 1024},
    {"name": "one_media_only", "image_max_edge": 1280, "max_media": 1, "max_output_tokens": 1024},
    {"name": "combined_768px_1media_1024tokens", "image_max_edge": 768, "max_media": 1, "max_output_tokens": 1024},
    {"name": "combined_768px_1media_512tokens", "image_max_edge": 768, "max_media": 1, "max_output_tokens": 512},
]


def pick_sample(posts: list[dict], limit: int = 5) -> list[tuple[int, dict]]:
    singles = []
    carousels = []
    for index, post in enumerate(posts, 1):
        media = post.get("media_files") or []
        images = [item for item in media if isinstance(item, dict) and item.get("media_type") == "IMAGE"]
        if not images:
            continue
        if len(media) == 1:
            singles.append((index, post))
        elif len(media) > 1:
            carousels.append((index, post))

    selected: list[tuple[int, dict]] = []
    if singles:
        for position in (0, len(singles) // 2, len(singles) - 1):
            candidate = singles[position]
            if candidate not in selected:
                selected.append(candidate)

    carousels.sort(key=lambda pair: len(pair[1].get("media_files") or []))
    for position in (0, len(carousels) - 1):
        if carousels:
            candidate = carousels[position]
            if candidate not in selected:
                selected.append(candidate)

    if len(selected) < limit:
        for candidate in singles + carousels:
            if candidate not in selected:
                selected.append(candidate)
                if len(selected) >= limit:
                    break
    return selected[:limit]


def scenario_metrics(
    name: str, settings: dict, outcomes: list[dict], errors: list[str],
    failed_usages: list[dict], wall_seconds: float,
) -> dict:
    usages = [item["usage"] for item in outcomes if isinstance(item.get("usage"), dict)] + failed_usages
    usage = analyzer.summarize_usage(usages)
    successful = len(outcomes)
    durations = [float(item["usage"].get("duration_seconds", 0)) for item in outcomes]
    return {
        "name": name,
        "settings": settings,
        "successes": successful,
        "failures": len(errors),
        "attempted_requests": successful + len(errors),
        "errors": errors,
        "wall_seconds": round(wall_seconds, 3),
        "mean_request_seconds": round(statistics.mean(durations), 3) if durations else None,
        "p95_request_seconds": round(sorted(durations)[min(len(durations) - 1, int(len(durations) * 0.95))], 3) if durations else None,
        "media_attached": sum(len(item.get("media_analyzed", [])) for item in outcomes),
        "mean_media_payload_kb_per_post": round(
            statistics.mean([item.get("media_payload_bytes", 0) for item in outcomes]) / 1024, 2
        ) if outcomes else None,
        "mean_response_words": round(statistics.mean([len(item.get("analysis", "").split()) for item in outcomes]), 1)
        if outcomes else None,
        "usage": usage,
    }


def percent_change(value: float | int | None, baseline: float | int | None) -> str:
    if value is None or baseline in (None, 0):
        return "n/a"
    return f"{(float(value) / float(baseline) - 1) * 100:+.1f}%"


def render_markdown(report: dict, json_path: Path) -> str:
    account = str(report.get("account") or "profile")
    scenarios = report.get("scenarios") or []
    rows = [
        "# Analyzer optimization benchmark",
        "",
        f"- Profile: @{account.lstrip('@')}",
        f"- Model: `{report.get('model')}`",
        f"- Fixed sample: {report.get('sample_size')} posts; media counts {report.get('sample_media_counts')}",
        f"- Concurrent requests: {report.get('worker_count')}",
        "- Raw post text, media, identifiers, and generated per-post analyses were not saved.",
        "- Quality was not formally scored; assess the result report before adopting lower resolution.",
        "",
        "| Scenario | Wall (s) | Avg request (s) | Input tokens | Output tokens | Media attached | Mean media KB/post | Cost USD | Failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in scenarios:
        usage = item["usage"]
        rows.append(
            f"| {item['name']} | {item['wall_seconds']} | {item['mean_request_seconds']} | "
            f"{usage.get('total_input_tokens')} | {usage.get('output_tokens')} | {item['media_attached']} | "
            f"{item['mean_media_payload_kb_per_post']} | {usage.get('cost_usd')} | {item['failures']} |"
        )

    baseline = next((item for item in scenarios if item["name"] == "baseline_1280px_3media_1024tokens"), None)
    if baseline:
        base_usage = baseline["usage"]
        rows += [
            "",
            "Relative to baseline (negative means lower; per-request cost is an estimate from recorded usage):",
            "",
            "| Scenario | Wall time | Input tokens | Mean media payload | Estimated cost |",
            "|---|---:|---:|---:|---:|",
        ]
        for item in scenarios:
            if item is baseline:
                continue
            usage = item["usage"]
            rows.append(
                f"| {item['name']} | {percent_change(item['wall_seconds'], baseline['wall_seconds'])} | "
                f"{percent_change(usage.get('total_input_tokens'), base_usage.get('total_input_tokens'))} | "
                f"{percent_change(item.get('mean_media_payload_kb_per_post'), baseline.get('mean_media_payload_kb_per_post'))} | "
                f"{percent_change(usage.get('cost_usd'), base_usage.get('cost_usd'))} |"
            )
        rows += [
            "",
            "One five-post pass is too small to establish stable wall-time differences; provider latency can dominate.",
            "The 512-token case is isolated against the matching 1024-token case in this report.",
        ]
    rows += ["", f"Detailed aggregate metrics: `{json_path.name}`.", ""]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small paid A/B benchmark of analyzer settings.")
    parser.add_argument("export", nargs="?", help="export folder containing posts.json")
    parser.add_argument("--posts", type=int, default=5, choices=range(3, 9), help="fixed sample size (3–8)")
    parser.add_argument("--scenario", action="append", choices=[item["name"] for item in SCENARIOS], help="run one named scenario; repeat to run several")
    parser.add_argument("--append-to", help="append these results to an existing benchmark JSON in the same export")
    parser.add_argument("--allow-contributor-training", action="store_true", help="confirm the existing Contributor data-use opt-in")
    args = parser.parse_args()

    analyzer.load_dotenv(ROOT / ".env")
    if not args.allow_contributor_training:
        print("Blocked: pass --allow-contributor-training to send benchmark media and captions.", file=sys.stderr)
        return 2
    api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not api_key:
        print("OPENCODE_API_KEY is missing from the local .env.", file=sys.stderr)
        return 2

    try:
        export_dir = analyzer.locate_export(args.export)
        payload = json.loads((export_dir / "posts.json").read_text(encoding="utf-8"))
        posts = payload.get("posts")
        if not isinstance(posts, list):
            raise ValueError("posts.json has no posts list")
    except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError) as exc:
        print(f"Could not read export: {exc}", file=sys.stderr)
        return 2

    selected = pick_sample(posts, args.posts)
    if len(selected) < 3:
        print("Need at least three posts with image media for this benchmark.", file=sys.stderr)
        return 2

    account = payload.get("account")
    if isinstance(account, dict):
        account = account.get("username") or account.get("name")
    account = str(account or "profile")
    model = os.environ.get("OPENCODE_MODEL", analyzer.DEFAULT_MODEL).strip() or analyzer.DEFAULT_MODEL
    workers = min(analyzer.setting_int("OPENCODE_MAX_CONCURRENT_POST_ANALYSES", 6, 1, 8), len(selected))
    scenarios = [item for item in SCENARIOS if not args.scenario or item["name"] in args.scenario]
    results = []
    sample_positions = [index for index, _ in selected]
    if args.append_to:
        json_path = Path(args.append_to).expanduser().resolve()
        if not json_path.is_relative_to(export_dir.resolve()) or not json_path.is_file():
            parser.error("--append-to must name an existing benchmark JSON inside the selected export")
        report = json.loads(json_path.read_text(encoding="utf-8"))
        if (
            report.get("sample_post_positions") != sample_positions
            or report.get("model") != model
            or report.get("account") != account
        ):
            parser.error("benchmark export, model, or fixed sample does not match --append-to")
        results = report.setdefault("scenarios", [])
        report["last_updated_at"] = datetime.now(timezone.utc).isoformat()
        md_path = json_path.with_suffix(".md")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = export_dir / f"optimization_benchmark_{stamp}.json"
        md_path = json_path.with_suffix(".md")
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "model": model,
            "sample_size": len(selected),
            "sample_post_positions": sample_positions,
            "sample_media_counts": [len(post.get("media_files") or []) for _, post in selected],
            "worker_count": workers,
            "quality_scoring": "Not scored; raw captions, images, identifiers, and analyses were not saved.",
            "scenarios": results,
        }

    print(
        f"Benchmark: {len(selected)} posts x {len(scenarios)} settings = "
        f"up to {len(selected) * len(scenarios)} paid model requests; concurrency {workers}.",
        flush=True,
    )
    for scenario in scenarios:
        analyzer.IMAGE_MAX_EDGE = scenario["image_max_edge"]
        started = time.monotonic()
        outcomes = []
        errors = []
        failed_usages = []
        session_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    analyzer.analyze_post,
                    api_key, model, session_id, export_dir, post, index,
                    scenario["max_media"], "minimal", scenario["max_output_tokens"],
                ): index
                for index, post in selected
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    errors.append(f"sample_position_{index}: {type(exc).__name__}: {exc}")
                    failed_usage = getattr(exc, "usage", None)
                    if isinstance(failed_usage, dict):
                        failed_usages.append(failed_usage)
                print(
                    f"{scenario['name']}: {len(outcomes) + len(errors)}/{len(selected)} complete",
                    flush=True,
                )
        wall_seconds = time.monotonic() - started
        metric = scenario_metrics(
            scenario["name"], scenario, outcomes, errors, failed_usages, wall_seconds
        )
        previous_index = next((i for i, item in enumerate(results) if item["name"] == scenario["name"]), None)
        if previous_index is None:
            results.append(metric)
        else:
            results[previous_index] = metric
        analyzer.write_json_atomic(json_path, report)
        print(
            f"{scenario['name']}: {metric['usage'].get('cost_usd')} USD, "
            f"{metric['wall_seconds']}s, {metric['usage'].get('total_input_tokens')} input tokens, "
            f"{metric['usage'].get('output_tokens')} output tokens.",
            flush=True,
        )

    md_path.write_text(render_markdown(report, json_path), encoding="utf-8")
    print(f"Saved benchmark report: {md_path}")
    print(f"Saved aggregate JSON: {json_path}")
    return 0 if all(item["failures"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
