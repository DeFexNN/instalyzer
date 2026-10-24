"""Session-isolated background jobs for the Streamlit interface.

The existing CLIs run in separate processes, so credentials and analyzer globals
cannot bleed between concurrent Streamlit sessions. No Streamlit API runs here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import queue
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from analysis_prompts import MAX_POSTS, DEFAULT_MAX_COMMENTS, comments_need_collection

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
REQUIRED = ("IG_USERNAME", "IG_SESSIONID", "IG_CSRFTOKEN", "IG_DS_USER_ID", "OPENCODE_API_KEY")
CONFIG_KEYS = (*REQUIRED, "APP_PASSWORD", "APP_ENABLE_LIVE", "APP_MAX_RUNS_PER_DAY", "APP_LOW_CPU_MODE",
    "INSTAGRAM_SCRAPER_PROVIDER", "APIFY_API_TOKEN", "APIFY_COMMENTS_MODE", "APIFY_MAX_TOTAL_CHARGE_USD",
    "HIKERAPI_TOKEN", "HIKERAPI_BASE_URL", "HIKERAPI_MAX_REQUESTS", "HIKERAPI_COMMENTS",
    "IG_MID", "IG_IG_DID", "IG_RUR", "IG_DATR", "IG_DPR", "IG_IG_NRCB",
    "IG_PS_L", "IG_PS_N", "IG_RD_CHALLENGE", "IG_WD", "OPENCODE_MODEL",
    "OPENCODE_IMAGE_MAX_EDGE", "OPENCODE_MAX_MEDIA_PER_POST", "OPENCODE_POST_MAX_OUTPUT_TOKENS",
    "OPENCODE_MAX_CONCURRENT_POST_ANALYSES", "OPENCODE_POST_REASONING_EFFORT",
    "OPENCODE_SYNTHESIS_REASONING_EFFORT", "IG_MEDIA_DOWNLOAD_WORKERS", "PLAYWRIGHT_CHROMIUM_EXECUTABLE",
    "IG_MAX_COMMENTS_PER_POST", "OPENCODE_MAX_COMMENTS_PER_POST", "OPENCODE_COMMENT_CHAR_BUDGET",
    "IG_COMPRESS_VIDEOS", "FFMPEG_THREADS", "OPENCODE_MEDIA_PREPARATION_WORKERS")
TERMINAL = {"complete", "failed", "cancelled"}
JOB_TIMEOUT_SECONDS = 1800
# Initial bounds are deliberately broad; these are not measured cloud timings.
INITIAL_DURATION_RANGE = (300, 900)


def read_config(secrets: Mapping | None = None) -> dict[str, str]:
    """Local .env < process environment < Streamlit Secrets; never mutate os.environ."""
    values: dict[str, str] = {}
    local = ROOT / ".env"
    if local.is_file():
        for line in local.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key.strip() in CONFIG_KEYS:
                values[key.strip()] = value
    for key in CONFIG_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
        if secrets is not None and key in secrets:
            values[key] = str(secrets[key])
    return values


def collection_provider(config: Mapping[str, str]) -> str:
    selected = str(config.get("INSTAGRAM_SCRAPER_PROVIDER", "auto")).strip().casefold()
    if selected == "auto":
        if str(config.get("HIKERAPI_TOKEN", "")).strip():
            return "hiker"
        return "apify" if str(config.get("APIFY_API_TOKEN", "")).strip() else "instagram"
    if selected not in {"hiker", "apify", "instagram"}:
        raise ValueError("INSTAGRAM_SCRAPER_PROVIDER: auto, hiker, apify or instagram")
    return selected


def live_configuration(config: Mapping[str, str]) -> tuple[bool, list[str]]:
    try:
        provider = collection_provider(config)
    except ValueError:
        return False, ["INSTAGRAM_SCRAPER_PROVIDER (auto / hiker / apify / instagram)"]
    required = {
        "hiker": ("HIKERAPI_TOKEN", "OPENCODE_API_KEY"),
        "apify": ("APIFY_API_TOKEN", "OPENCODE_API_KEY"),
    }.get(provider, REQUIRED)
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if len(config.get("APP_PASSWORD", "")) < 12:
        missing.append("APP_PASSWORD (мінімум 12 символів)")
    enabled = str(config.get("APP_ENABLE_LIVE", "true")).lower() in {"true", "1", "yes"}
    return enabled and not missing, missing


def apply_resource_limits(env: dict[str, str], config: Mapping[str, str]) -> bool:
    low_cpu = str(config.get("APP_LOW_CPU_MODE", "true")).strip().casefold() in {"1", "true", "yes", "on"}
    if low_cpu:
        env.update({"IG_COMPRESS_VIDEOS": "false",
                    "FFMPEG_THREADS": "1", "OPENCODE_MEDIA_PREPARATION_WORKERS": "1"})
        try:
            env["IG_MEDIA_DOWNLOAD_WORKERS"] = str(max(1, min(2, int(env.get("IG_MEDIA_DOWNLOAD_WORKERS", "2")))))
        except ValueError:
            env["IG_MEDIA_DOWNLOAD_WORKERS"] = "2"
    return low_cpu


def password_signature(config: Mapping[str, str]) -> str:
    return hashlib.sha256(config.get("APP_PASSWORD", "").encode()).hexdigest()


def password_matches(candidate: str, config: Mapping[str, str]) -> bool:
    expected = config.get("APP_PASSWORD", "")
    return len(expected) >= 12 and hmac.compare_digest(candidate.encode(), expected.encode())


def normalize_profile(value: str) -> str:
    from scrape_instagram import profile_username_from_input
    try:
        return profile_username_from_input(value)
    except RuntimeError:
        raise ValueError("Вкажи нік або посилання на профіль: instagram.com/username/") from None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, round(seconds))
    return f"{seconds} с" if seconds < 60 else f"{seconds // 60} хв {seconds % 60:02d} с"


def format_wait_range(lower: float, upper: float) -> str:
    """Use rounded ranges rather than suggesting second-level ETA accuracy."""
    if upper < 60:
        return f"до {max(5, math.ceil(upper / 5) * 5)} с"
    minimum = max(0, math.floor(lower / 60))
    maximum = max(minimum + 1, math.ceil(upper / 60))
    return f"{minimum}–{maximum} хв" if minimum else f"до {maximum} хв"


def result_cost(payload: dict) -> float | None:
    # model_usage already includes summary_usage in current analyzer reports.
    usage = payload.get("model_usage") or {}
    value = usage.get("cost_usd")
    if value is None or not usage.get("cost_is_complete", True):
        return None
    collection = payload.get("collection_usage") or {}
    source_cost = collection.get("cost_usd", 0)
    if source_cost is None or not collection.get("cost_is_complete", True):
        return None
    return float(value) + float(source_cost)


@dataclass
class Job:
    id: str
    owner: str
    username: str
    state: str = "queued"
    stage: str = "queued"
    done: int = 0
    total: int = MAX_POSTS
    created: float = field(default_factory=time.time)
    queued_at: float | None = None
    started: float | None = None
    finished: float | None = None
    export_dir: Path | None = None
    error: str = ""
    resumed: bool = False
    logs: deque = field(default_factory=lambda: deque(maxlen=160), repr=False)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)


class JobManager:
    def __init__(self, runtime: Path = RUNTIME):
        self.runtime = runtime.resolve()
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="instalyzer")
        self.login_attempts: dict[str, tuple[int, float]] = {}

    def unlock(self, owner: str, password: str, config: Mapping[str, str]) -> bool:
        with self.lock:
            count, until = self.login_attempts.get(owner, (0, 0))
            if until > time.time():
                raise ValueError("Забагато спроб. Спробуй через хвилину.")
            if password_matches(password, config):
                self.login_attempts.pop(owner, None)
                return True
            count += 1
            self.login_attempts[owner] = (count if count < 5 else 0, time.time() + 60 if count >= 5 else 0)
            return False

    def _owned(self, owner: str, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.owner != owner:
            raise ValueError("Результат недоступний у цій сесії.")
        return job

    def _reserve_budget(self, config: Mapping[str, str]) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        budget_path = self.runtime / "budget.json"
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            saved = json.loads(budget_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        used = int(saved.get("runs", 0)) if saved.get("day") == today else 0
        try:
            limit = max(1, min(100, int(config.get("APP_MAX_RUNS_PER_DAY", "10"))))
        except ValueError:
            limit = 10
        if used >= limit:
            raise ValueError("На сьогодні демо досягло ліміту запусків. Готові звіти залишаються доступними.")
        total_bytes = 0
        for entry in self.runtime.rglob("*"):
            try:
                if entry.is_file():
                    total_bytes += entry.stat().st_size
            except OSError:
                # A running encoder can atomically replace a file during the scan.
                continue
        if total_bytes > 1024 * 1024 * 1024:
            raise ValueError("Сховище демо заповнене. Збережи результати й перезапусти застосунок.")
        temporary = budget_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"day": today, "runs": used + 1}), encoding="utf-8")
        temporary.replace(budget_path)

    def submit(self, owner: str, profile: str, config: Mapping[str, str], *, authorized: bool) -> str:
        if not authorized or not live_configuration(config)[0]:
            raise ValueError("Спочатку відкрий доступ до запусків.")
        username = normalize_profile(profile)
        with self.lock:
            if any(j.owner == owner and j.state not in TERMINAL for j in self.jobs.values()):
                raise ValueError("У цій сесії вже є активний аналіз.")
            if sum(j.state not in TERMINAL for j in self.jobs.values()) >= 4:
                raise ValueError("Черга заповнена. Спробуй трохи пізніше.")
            self._reserve_budget(config)
            job = Job(uuid.uuid4().hex, owner, username)
            self.jobs[job.id] = job
            self.pool.submit(self._run, job, dict(config))
            return job.id

    def snapshot(self, owner: str, job_id: str) -> dict:
        with self.lock:
            job = self._owned(owner, job_id)
            now = time.time()
            end = job.finished if job.finished is not None else now
            return {"id": job.id, "username": job.username, "state": job.state, "stage": job.stage,
                "done": job.done, "total": job.total, "error": job.error,
                "elapsed": end - job.started if job.started is not None else 0, "created": job.created,
                "queue_elapsed": max(0, now - (job.queued_at if job.queued_at is not None else job.created)) if job.state == "queued" else 0,
                "can_resume": job.state == "failed" and self._has_checkpoint(job),
                **self._queue_snapshot(job, now)}

    def _has_checkpoint(self, job: Job) -> bool:
        return job.export_dir is not None or any((self.runtime / job.id / "exports").glob("*/.apify/runs.json"))

    def _duration_range(self, job: Job) -> tuple[float, float, int]:
        samples = [j for j in self.jobs.values() if j.state == "complete" and not j.resumed
                   and j.started is not None and j.finished is not None
                   and 1 <= j.finished - j.started <= JOB_TIMEOUT_SECONDS and j.total > 0][-10:]
        post_count = max(1, min(MAX_POSTS, job.total))
        if not samples:
            scale = post_count / MAX_POSTS
            return max(60, INITIAL_DURATION_RANGE[0] * scale), max(180, INITIAL_DURATION_RANGE[1] * scale), 0
        per_post = statistics.median((j.finished - j.started) / j.total for j in samples)
        predicted = per_post * post_count
        lower = min(JOB_TIMEOUT_SECONDS - 30, max(10, predicted * 0.7))
        upper = min(JOB_TIMEOUT_SECONDS, max(lower + 30, predicted * 1.6))
        return lower, upper, len(samples)

    def _queue_snapshot(self, job: Job, now: float) -> dict:
        # Expose counts and timing only; never another session's profile or job ID.
        running = [j for j in self.jobs.values() if j.state == "running"]
        pending = sorted((j for j in self.jobs.values() if j.state == "queued" and not j.cancel.is_set()),
                         key=lambda j: j.queued_at if j.queued_at is not None else j.created)
        ordered = running + pending
        position = next((i for i, item in enumerate(ordered) if item.id == job.id), None)
        result = {"queue_position": position + 1 if position is not None else None,
            "queue_total": len(ordered), "jobs_ahead": position or 0, "running_jobs": len(running),
            "wait_seconds_min": None, "wait_seconds_max": None,
            "wait_estimate_basis": None, "wait_estimate_samples": 0, "wait_estimate_overdue": False}
        if job.state != "queued" or position is None:
            return result
        lower, upper = 0.0, 0.0
        sample_count = 0
        for ahead in ordered[:position]:
            low, high, samples = self._duration_range(ahead)
            sample_count = max(sample_count, samples)
            if ahead.cancel.is_set():
                low, high = 0, 15
            elif ahead.state == "running" and ahead.started is not None:
                elapsed = max(0, now - ahead.started)
                result["wait_estimate_overdue"] |= elapsed >= high
                # An overrun must not turn into a spurious zero-second promise.
                low = max(0, low - elapsed)
                high = min(max(10, high - elapsed, high * 0.25), max(10, JOB_TIMEOUT_SECONDS - elapsed + 15))
            lower += low
            upper += high
        result.update({"wait_seconds_min": round(lower), "wait_seconds_max": round(max(10, upper)),
            "wait_estimate_basis": "history" if sample_count else "initial",
            "wait_estimate_samples": sample_count})
        return result

    def history(self, owner: str) -> list[dict]:
        with self.lock:
            return [self.snapshot(owner, j.id) for j in reversed(list(self.jobs.values())) if j.owner == owner]

    def cancel(self, owner: str, job_id: str) -> None:
        with self.lock:
            job = self._owned(owner, job_id)
            if job.state not in TERMINAL:
                job.cancel.set()
                if job.state == "queued":
                    job.state, job.stage, job.error = "cancelled", "cancelled", "Аналіз зупинено."
                    job.finished = time.time()

    def resume(self, owner: str, job_id: str, config: Mapping[str, str], *, authorized: bool) -> None:
        if not authorized or not live_configuration(config)[0]:
            raise ValueError("Спочатку відкрий доступ до запусків.")
        with self.lock:
            job = self._owned(owner, job_id)
            if job.state != "failed" or not self._has_checkpoint(job):
                raise ValueError("Цей аналіз не можна продовжити.")
            if any(j.state not in TERMINAL for j in self.jobs.values()):
                raise ValueError("Дочекайся завершення активного аналізу.")
            self._reserve_budget(config)
            job.state, job.stage, job.error = "queued", "queued", ""
            job.queued_at = time.time()
            job.resumed = True
            job.started = job.finished = None
            job.cancel.clear()
            self.pool.submit(self._run, job, dict(config))

    def artifact(self, owner: str, job_id: str, name: str) -> bytes:
        if name not in {"profile_analysis.md", "profile_analysis.json", "posts.json"}:
            raise ValueError("Невідомий файл звіту.")
        with self.lock:
            job = self._owned(owner, job_id)
            if job.state != "complete" or not job.export_dir:
                raise ValueError("Звіт ще не готовий.")
            return (job.export_dir / name).read_bytes()

    def result(self, owner: str, job_id: str) -> dict:
        return json.loads(self.artifact(owner, job_id, "profile_analysis.json"))

    def media_preview(self, owner: str, job_id: str) -> list[Path]:
        with self.lock:
            job = self._owned(owner, job_id)
            if job.state != "complete" or not job.export_dir:
                return []
            manifest = json.loads((job.export_dir / "posts.json").read_text(encoding="utf-8"))
            folder = (job.export_dir / "media").resolve()
            paths = []
            for post in manifest.get("posts", []):
                for item in post.get("media_files", []):
                    path = (folder / str(item.get("file") or "")).resolve()
                    if path.is_relative_to(folder) and path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                        paths.append(path)
            return paths[:12]

    def _update(self, job: Job, **values) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(job, key, value)

    def _record(self, job: Job, line: str, config: Mapping[str, str]) -> None:
        for key, value in config.items():
            if value and len(value) >= 8 and any(part in key for part in ("SESSION", "TOKEN", "KEY", "PASSWORD", "CHALLENGE")):
                line = line.replace(value, "[hidden]")
        with self.lock:
            job.logs.append(line[:1000])
            match = re.search(r"Prepared (\d+)/(\d+)", line)
            if match:
                job.stage, job.done, job.total = "media", int(match[1]), int(match[2])
            match = re.search(r"Collected comments (\d+)/(\d+)", line)
            if match:
                job.stage, job.done, job.total = "comments", int(match[1]), int(match[2])
            match = re.search(r"Готово: (\d+)/(\d+)", line)
            if match:
                job.done, job.total = int(match[1]), int(match[2])
                job.stage = "synthesis" if job.done == job.total else "analysis"
            match = re.search(r"Пости: (\d+); вже є в checkpoint: (\d+)", line)
            if match:
                job.total, job.done = int(match[1]), int(match[2])
                job.stage = "synthesis" if job.done == job.total else "analysis"

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=4)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    def _process(self, job: Job, script: str, arguments: list[str], env: dict, config: Mapping[str, str]) -> None:
        def abort_remote():
            if script == "scrape_instagram_apify.py":
                from scrape_instagram_apify import abort_export_runs
                try:
                    abort_export_runs(env["IG_OUTPUT_DIR"], env.get("APIFY_API_TOKEN", ""))
                except (RuntimeError, OSError, ValueError):
                    pass  # The provider's spending and time caps still apply.
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with subprocess.Popen([sys.executable, "-u", str(ROOT / script), *arguments], cwd=ROOT,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", **options) as process:
            lines: queue.Queue[str] = queue.Queue()
            def read_lines():
                assert process.stdout is not None
                for line in process.stdout:
                    lines.put(line.rstrip())
            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            while process.poll() is None or reader.is_alive() or not lines.empty():
                if job.cancel.is_set():
                    abort_remote()
                    self._terminate(process)
                    raise InterruptedError("Аналіз зупинено.")
                if time.time() - (job.started or time.time()) > JOB_TIMEOUT_SECONDS:
                    abort_remote()
                    self._terminate(process)
                    raise TimeoutError("Час очікування перевищено. Спробуй ще раз пізніше.")
                try:
                    self._record(job, lines.get(timeout=0.2), config)
                except queue.Empty:
                    pass
            if process.returncode != 0:
                messages = "\n".join(job.logs).lower()
                if script == "scrape_instagram_apify.py":
                    raise RuntimeError("Apify не завершив збір. Перевір токен, баланс і доступність публічного профілю; збережений запуск можна продовжити.")
                if script == "scrape_instagram_hiker.py":
                    raise RuntimeError("HikerAPI не завершив збір. Перевір HIKERAPI_TOKEN, баланс і HIKERAPI_MAX_REQUESTS; повторний запуск використає кеш.")
                if any(word in messages for word in ("cookie", "logged out", "session", "checkpoint_required", "challenge_required")):
                    raise RuntimeError("Instagram не прийняв сеанс. Власнику демо потрібно оновити cookies у Secrets.")
                if "429" in messages:
                    raise RuntimeError("Сервіс тимчасово обмежив частоту запитів. Спробуй пізніше.")
                if "api" in messages or "opencode" in messages:
                    raise RuntimeError("Модель не завершила аналіз. Збережений прогрес можна продовжити.")
                raise RuntimeError("Не вдалося завершити збір матеріалів. Перевір доступність профілю та спробуй пізніше.")

    def _run(self, job: Job, config: dict[str, str]) -> None:
        with self.lock:
            if job.state == "cancelled":
                return
            self._update(job, state="running", started=time.time(), stage="scraping",
                         resumed=job.resumed or self._has_checkpoint(job))
        try:
            if job.cancel.is_set():
                raise InterruptedError("Аналіз зупинено.")
            work_dir = self.runtime / job.id
            work_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update({k: v for k, v in config.items() if k in CONFIG_KEYS and not k.startswith("APP_")})
            env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "IG_INCLUDE_COMMENTS": "true",
                "IG_OUTPUT_DIR": str(work_dir / "exports")})
            env.setdefault("IG_MEDIA_DOWNLOAD_WORKERS", "2")
            env.setdefault("IG_MAX_COMMENTS_PER_POST", str(DEFAULT_MAX_COMMENTS))
            env.setdefault("OPENCODE_MAX_CONCURRENT_POST_ANALYSES", "4")
            low_cpu_mode = apply_resource_limits(env, config)
            # Preserve the richer web analysis even when an existing deployment
            # still has the earlier 1024-token / three-media Secrets values.
            for key, minimum, maximum in (("OPENCODE_POST_MAX_OUTPUT_TOKENS", 2048, 4096),
                                          ("OPENCODE_MAX_MEDIA_PER_POST", 6, 20)):
                try:
                    env[key] = str(min(maximum, max(minimum, int(env.get(key, str(minimum))))))
                except ValueError:
                    env[key] = str(minimum)
            scrape_started = time.monotonic()
            resumed = job.resumed
            collection_ran = job.export_dir is None
            provider = collection_provider(config)
            if job.export_dir is None:
                script = {"apify": "scrape_instagram_apify.py",
                          "hiker": "scrape_instagram_hiker.py"}.get(provider, "scrape_instagram.py")
                arguments = [f"https://www.instagram.com/{job.username}/"]
                if provider == "apify":
                    arguments.extend(["--limit", str(MAX_POSTS), "--comments-mode", config.get("APIFY_COMMENTS_MODE", "preview")])
                elif provider == "hiker":
                    comments_enabled = str(config.get("HIKERAPI_COMMENTS", "false")).strip().casefold() in {
                        "1", "true", "yes", "on"}
                    arguments.extend(["--limit", str(MAX_POSTS)])
                    if comments_enabled:
                        arguments.extend(["--comments", "--max-comments",
                                          env.get("IG_MAX_COMMENTS_PER_POST", str(DEFAULT_MAX_COMMENTS))])
                    try:
                        max_requests = max(1, int(config.get("HIKERAPI_MAX_REQUESTS", "40") or 40))
                    except ValueError:
                        max_requests = 40
                    if comments_enabled:
                        max_requests = max(max_requests, MAX_POSTS + 15)
                    arguments.extend(["--max-requests", str(max_requests)])
                self._process(job, script, arguments, env, config)
                manifests = sorted((work_dir / "exports").glob("*/posts.json"))
                if not manifests:
                    raise RuntimeError("Instagram не повернув матеріалів для цього профілю.")
                job.export_dir = manifests[-1].parent
            payload = json.loads((job.export_dir / "posts.json").read_text(encoding="utf-8"))
            source_provider = {"hikerapi": "hiker", "apify": "apify"}.get(
                str(payload.get("provider", "")).casefold(), "instagram")
            posts = payload.get("posts", [])[:MAX_POSTS]
            if not posts or not all(isinstance(p, dict) for p in posts):
                raise RuntimeError("У профілі немає доступних публікацій.")
            # Resuming an export made before comment collection was enabled must
            # collect those comments before the model sees the old source data.
            # HikerAPI exports always carry an explicit comments_status, and its
            # collector never falls back to Instagram session cookies.
            missing_comments = source_provider != "hiker" and any(comments_need_collection(p) for p in posts)
            if missing_comments:
                self._update(job, stage="comments", done=0, total=len(posts))
                script = "scrape_instagram_apify.py" if source_provider == "apify" else "scrape_instagram.py"
                self._process(job, script, ["--collect-comments", str(job.export_dir)], env, config)
                payload = json.loads((job.export_dir / "posts.json").read_text(encoding="utf-8"))
                posts = payload.get("posts", [])[:MAX_POSTS]
                if not posts or any(not isinstance(p, dict) or comments_need_collection(p) for p in posts):
                    raise RuntimeError("Не вдалося перевірити збір коментарів. Продовж запуск або створи новий аналіз.")
            scrape_seconds = time.monotonic() - scrape_started if collection_ran or missing_comments else 0.0
            if not any(m.get("file") for p in posts for m in p.get("media_files", [])):
                raise RuntimeError("Не вдалося завантажити медіа для аналізу.")
            self._update(job, stage="analysis", done=0, total=len(posts))
            self._process(job, "analyze_instagram_profile.py", [str(job.export_dir), "--allow-contributor-training"], env, config)
            report_path = job.export_dir / "profile_analysis.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["collection_usage"] = payload.get("provider_usage") or {}
            report["collection_coverage"] = payload.get("coverage") or {}
            report["collection_warnings"] = payload.get("warnings") or []
            report["web_execution"] = {
                "total_duration_seconds": round(time.time() - job.started, 3),
                "scrape_duration_seconds": round(scrape_seconds, 3),
                "analysis_includes_media_preparation": True,
                "resumed": resumed,
                "low_cpu_mode": low_cpu_mode,
                "scraper_provider": source_provider,
            }
            temporary = report_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(report_path)
            self._update(job, stage="complete", state="complete", finished=time.time(), done=len(posts))
        except InterruptedError as exc:
            self._update(job, state="cancelled", stage="cancelled", error=str(exc), finished=time.time())
        except Exception as exc:
            # Raw exceptions and logs stay server-side; the public UI receives curated errors only.
            safe = str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else "Сталася помилка під час обробки. Спробуй ще раз."
            self._update(job, state="failed", stage="failed", error=safe, finished=time.time())
