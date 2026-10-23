"""Shared media normalization for compact Instagram exports and AI requests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageOps


IMAGE_MAX_EDGE = 1280
IMAGE_JPEG_QUALITY = 80
VIDEO_MAX_EDGE = 720
VIDEO_CRF = 28


def encode_image_jpeg(
    source: Path,
    *,
    max_edge: int = IMAGE_MAX_EDGE,
    quality: int = IMAGE_JPEG_QUALITY,
) -> bytes:
    """Resize an image if needed and encode it as an optimized quality-N JPEG."""
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        output = BytesIO()
        save_options = {
            "format": "JPEG",
            "quality": max(1, min(95, int(quality))),
            "optimize": True,
            "progressive": True,
            "subsampling": 2,
        }
        icc_profile = opened.info.get("icc_profile")
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        image.save(output, **save_options)
        return output.getvalue()


def compact_video_file(
    path: Path,
    *,
    max_edge: int = VIDEO_MAX_EDGE,
    crf: int = VIDEO_CRF,
    timeout_seconds: int = 600,
) -> bool:
    """Transcode a video to smaller H.264/AAC media, replacing it only when smaller."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path.is_file():
        return False
    original_size = path.stat().st_size
    temporary = path.with_name(f"{path.stem}.compact{path.suffix}")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
        "-vf", (
            f"scale=w='min({max_edge},iw)':h='min({max_edge},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        return False
    if temporary.stat().st_size >= original_size:
        temporary.unlink(missing_ok=True)
        return False
    temporary.replace(path)
    return True

