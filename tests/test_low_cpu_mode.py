from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import analyze_instagram_profile as analyzer
import media_processing
import scrape_instagram as scraper
from web_backend import JobManager, apply_resource_limits


class LowCpuTests(unittest.TestCase):
    def test_web_defaults_apply_low_cpu_limits_even_with_old_settings(self):
        env = {"IG_COMPRESS_VIDEOS": "true", "IG_MEDIA_DOWNLOAD_WORKERS": "6", "FFMPEG_THREADS": "4",
               "OPENCODE_MAX_CONCURRENT_POST_ANALYSES": "4"}
        self.assertTrue(apply_resource_limits(env, {}))
        self.assertEqual(env["IG_COMPRESS_VIDEOS"], "false")
        self.assertEqual(env["IG_MEDIA_DOWNLOAD_WORKERS"], "2")
        self.assertEqual(env["FFMPEG_THREADS"], "1")
        self.assertEqual(env["OPENCODE_MEDIA_PREPARATION_WORKERS"], "1")
        self.assertEqual(env["OPENCODE_MAX_CONCURRENT_POST_ANALYSES"], "4")

    def test_low_cpu_mode_can_be_disabled_explicitly(self):
        env = {"IG_COMPRESS_VIDEOS": "true", "FFMPEG_THREADS": "2"}
        self.assertFalse(apply_resource_limits(env, {"APP_LOW_CPU_MODE": "false"}))
        self.assertEqual(env, {"IG_COMPRESS_VIDEOS": "true", "FFMPEG_THREADS": "2"})

    def test_disabled_transcode_preserves_downloaded_video(self):
        data = b"fictional-video-bytes"
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {"Content-Type": "video/mp4"}
        response.geturl.return_value = "https://scontent.cdninstagram.com/clip.mp4"
        response.read.side_effect = [data, b""]
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"IG_COMPRESS_VIDEOS": "false"}), \
                patch.object(scraper.urllib.request, "urlopen", return_value=response), \
                patch.object(scraper, "compact_video_file") as compact:
            result = scraper.download_media(response.geturl(), Path(folder) / "video")
            self.assertEqual((Path(folder) / result[0]).read_bytes(), data)
            self.assertEqual(result[1:3], (len(data), len(data)))
            self.assertIsNone(result[3])
            compact.assert_not_called()

    def test_frame_extraction_limits_decoder_encoder_and_filter_threads(self):
        with patch.dict(os.environ, {"FFMPEG_THREADS": "1"}), \
                patch.object(analyzer.shutil, "which", return_value="ffmpeg"), \
                patch.object(analyzer.subprocess, "run", return_value=Mock(returncode=0, stdout=b"frame")) as run:
            self.assertEqual(analyzer.ffmpeg_encode(Path("video.mp4"), video=True), b"frame")
            command = run.call_args.args[0]
            self.assertEqual(command.count("-threads"), 2)
            self.assertEqual([command[i + 1] for i, x in enumerate(command) if x == "-threads"], ["1", "1"])
            self.assertEqual(command[command.index("-filter_threads") + 1], "1")

    def test_media_preparation_is_serial_but_model_requests_can_overlap(self):
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active, peak = 0, 0
        def prepare(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return "image/jpeg", b"fictional-image"
        def model(*args, **kwargs):
            barrier.wait(timeout=3)
            return "Fictional analysis", {}
        post = {"id": "1", "media_files": [{"file": "image.jpg"}], "comments": [], "comments_status": "available"}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(analyzer, "MEDIA_PREPARATION_SEMAPHORE", threading.Semaphore(1)), \
                patch.object(analyzer, "image_data", side_effect=prepare), \
                patch.object(analyzer, "call_model", side_effect=model), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(analyzer.analyze_post, "unused", "test", "offline", Path(folder), post, i, 6, "minimal", 2048) for i in (1, 2)]
            self.assertEqual([f.result()["analysis"] for f in futures], ["Fictional analysis"] * 2)
        self.assertEqual(peak, 1)

    def test_ffmpeg_threads_are_bounded(self):
        for value, expected in (("1", 1), ("50", 4), ("0", 1), ("invalid", 2)):
            with self.subTest(value=value), patch.dict(os.environ, {"FFMPEG_THREADS": value}):
                self.assertEqual(media_processing.ffmpeg_thread_count(), expected)

    def test_budget_scan_preserves_exports_and_enforces_daily_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = JobManager(Path(folder))
            try:
                media = Path(folder) / "existing-export" / "photo.jpg"
                media.parent.mkdir()
                media.write_bytes(b"original-photo")
                manager._reserve_budget({"APP_MAX_RUNS_PER_DAY": "1"})
                self.assertEqual(media.read_bytes(), b"original-photo")
                budget = json.loads((Path(folder) / "budget.json").read_text(encoding="utf-8"))
                self.assertEqual(budget["runs"], 1)
                with self.assertRaisesRegex(ValueError, "ліміту запусків"):
                    manager._reserve_budget({"APP_MAX_RUNS_PER_DAY": "1"})
            finally:
                manager.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
