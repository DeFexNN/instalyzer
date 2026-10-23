#!/usr/bin/env python3
"""Lossily compact media in an existing Instagram export, updating posts.json."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from media_processing import (
    IMAGE_JPEG_QUALITY,
    IMAGE_MAX_EDGE,
    VIDEO_CRF,
    VIDEO_MAX_EDGE,
    compact_video_file,
    encode_image_jpeg,
)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def compact_image(path: Path) -> tuple[Path, int, dict | None]:
    original_size = path.stat().st_size
    try:
        encoded = encode_image_jpeg(path)
    except (OSError, ValueError):
        return path, original_size, None
    if not encoded or len(encoded) >= original_size:
        return path, original_size, None

    destination = path.with_suffix(".jpg")
    temporary = destination.with_name(destination.name + ".compact")
    try:
        temporary.write_bytes(encoded)
        temporary.replace(destination)
        if destination != path:
            path.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, len(encoded), {
        "method": "jpeg",
        "quality": IMAGE_JPEG_QUALITY,
        "max_edge": IMAGE_MAX_EDGE,
    }


def compact_export(export_dir: Path) -> tuple[int, int, int]:
    posts_path = export_dir / "posts.json"
    payload = json.loads(posts_path.read_text(encoding="utf-8"))
    posts = payload.get("posts")
    if not isinstance(posts, list):
        raise ValueError("posts.json не містить списку posts")

    changed_count = 0
    bytes_before = 0
    bytes_after = 0
    media_root = (export_dir / "media").resolve()
    for post in posts:
        if not isinstance(post, dict):
            continue
        for media in post.get("media_files", []):
            if not isinstance(media, dict) or not media.get("file"):
                continue
            path = Path(str(media["file"]))
            if not path.is_absolute():
                if path.parts and path.parts[0].casefold() == "media":
                    path = export_dir / path
                else:
                    path = media_root / path
            path = path.resolve()
            if not path.is_relative_to(media_root):
                continue
            if not path.is_file():
                continue

            suffix = path.suffix.lower()
            compression = media.get("compression") or {}
            if suffix in IMAGE_SUFFIXES:
                if (
                    compression.get("method") == "jpeg"
                    and compression.get("quality") == IMAGE_JPEG_QUALITY
                    and compression.get("max_edge") == IMAGE_MAX_EDGE
                ):
                    continue
                before = path.stat().st_size
                compacted_path, after, metadata = compact_image(path)
            elif suffix in VIDEO_SUFFIXES:
                if (
                    compression.get("method") == "h264_crf"
                    and compression.get("crf") == VIDEO_CRF
                    and compression.get("max_edge") == VIDEO_MAX_EDGE
                ):
                    continue
                before = path.stat().st_size
                was_compacted = compact_video_file(path)
                after = path.stat().st_size
                metadata = (
                    {
                        "method": "h264_crf",
                        "crf": VIDEO_CRF,
                        "max_edge": VIDEO_MAX_EDGE,
                    }
                    if was_compacted else None
                )
                compacted_path = path
            else:
                continue

            if metadata:
                changed_count += 1
                bytes_before += before
                bytes_after += after
                try:
                    media["file"] = str(compacted_path.relative_to(export_dir / "media"))
                except ValueError:
                    media["file"] = str(compacted_path)
                media["bytes"] = after
                media["original_bytes"] = before
                media["compression"] = metadata
                write_json_atomic(posts_path, payload)

    return changed_count, bytes_before, bytes_after


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Стиснути фото до JPEG quality 80 / 1280 px і відео до 720 px / H.264 CRF 28."
    )
    parser.add_argument("export", help="каталог експорту з posts.json")
    args = parser.parse_args()
    export_dir = Path(args.export).expanduser().resolve()
    if not (export_dir / "posts.json").is_file():
        parser.error("у вказаному каталозі немає posts.json")
    if not os.access(export_dir, os.W_OK):
        parser.error("каталог експорту недоступний для запису")
    try:
        changed, before, after = compact_export(export_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Не вдалося стиснути експорт: {exc}")
        return 1
    print(f"Стиснуто файлів: {changed}")
    if changed:
        saved = before - after
        percent = saved * 100 / before if before else 0
        print(f"Було: {before / 1_000_000:.2f} MB; стало: {after / 1_000_000:.2f} MB.")
        print(f"Звільнено: {saved / 1_000_000:.2f} MB ({percent:.1f}%).")
    else:
        print("Медіафайли вже оптимізовані або не вдалося отримати меншу версію.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
