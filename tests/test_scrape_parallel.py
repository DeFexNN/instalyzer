from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import scrape_instagram


class BuildPostRecordsTests(unittest.TestCase):
    def _run_with_worker_measurement(self, *, post_count: int, requested_workers: int):
        lock = threading.Lock()
        active = 0
        peak_active = 0
        completion_order: list[int] = []

        def fake_post_to_record(post, index, media_dir, download, include_comments):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            # Different delays make completion order differ from source order.
            time.sleep((post_count - index + 1) * 0.003)
            with lock:
                active -= 1
                completion_order.append(index)
            return {"id": post["id"], "source_index": index}

        posts = [{"id": f"post-{index}"} for index in range(1, post_count + 1)]
        with patch.object(scrape_instagram, "post_to_record", side_effect=fake_post_to_record):
            records = scrape_instagram.build_post_records(
                posts,
                Path("unused-media"),
                download=True,
                include_comments=False,
                max_workers=requested_workers,
            )
        return records, peak_active, completion_order

    def test_parallel_processing_preserves_profile_order(self):
        records, peak_active, completion_order = self._run_with_worker_measurement(
            post_count=8,
            requested_workers=3,
        )

        self.assertEqual([record["source_index"] for record in records], list(range(1, 9)))
        self.assertGreater(peak_active, 1)
        self.assertLessEqual(peak_active, 3)
        self.assertNotEqual(completion_order, list(range(1, 9)))

    def test_parallelism_is_capped_at_configured_maximum(self):
        records, peak_active, _ = self._run_with_worker_measurement(
            post_count=10,
            requested_workers=scrape_instagram.MAX_MEDIA_DOWNLOAD_WORKERS + 10,
        )

        self.assertEqual(len(records), 10)
        self.assertGreater(peak_active, 1)
        self.assertLessEqual(peak_active, scrape_instagram.MAX_MEDIA_DOWNLOAD_WORKERS)


class DownloadRetryTests(unittest.TestCase):
    def test_transient_timeout_is_retried_once(self):
        with (
            patch.object(
                scrape_instagram,
                "download_media",
                side_effect=[RuntimeError("Could not download media: The read operation timed out."), ("photo.jpg", 12, 24, None)],
            ) as download,
            patch.object(scrape_instagram.time, "sleep") as sleep,
        ):
            result = scrape_instagram.download_media_with_retry(
                "https://scontent.cdninstagram.com/photo.jpg", Path("photo")
            )

        self.assertEqual(result, ("photo.jpg", 12, 24, None))
        self.assertEqual(download.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_permanent_http_error_is_not_retried(self):
        with patch.object(
            scrape_instagram,
            "download_media",
            side_effect=RuntimeError("Media download returned HTTP 403."),
        ) as download:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                scrape_instagram.download_media_with_retry(
                    "https://scontent.cdninstagram.com/photo.jpg", Path("photo")
                )

        download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
