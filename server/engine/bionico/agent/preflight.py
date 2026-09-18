"""Input preflight: probe, validate and normalize customer uploads.

Runs right after the agent downloads a job's input, BEFORE it ever reaches
the watcher/pipeline, so a corrupt, oversized or non-vertical upload fails
fast with a clear Spanish reason instead of burning a full pipeline attempt
(real incident: 5 jobs failed mid-pipeline on 'moov atom not found').

Also normalizes .mov/.m4v uploads to .mp4 at ingest (ffmpeg stream copy,
re-encode fallback) so the watcher and pipeline keep seeing .mp4 only.

When ffmpeg/ffprobe are not installed the checks are skipped (the pipeline
itself needs ffmpeg, so this only happens on broken installs); the file is
handed through unchanged rather than blocking real work.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# CREATE_NO_WINDOW: keep ffprobe/ffmpeg from flashing a console on Windows.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PROBE_TIMEOUT = 120
REMUX_TIMEOUT = 600
REENCODE_TIMEOUT = 3600

# Extensions the cloud may hand us; everything else is treated as .mp4.
ACCEPTED_EXTS = (".mp4", ".mov", ".m4v")


def max_video_seconds() -> float:
    try:
        return float(os.environ.get("MAX_VIDEO_SECONDS") or 420)
    except ValueError:
        return 420.0


class PreflightRejected(Exception):
    """The upload is unusable; `reason` is a short customer-facing Spanish
    sentence (posted verbatim as the job's error)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _run(binary: str, args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [binary, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def probe(path: Path) -> tuple[float, int, int] | None:
    """(duration_seconds, display_width, display_height) of the first video
    stream, rotation-corrected. None when the file is unreadable/corrupt
    (e.g. truncated upload with no moov atom) or has no video stream."""
    res = _run("ffprobe", [
        "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=PROBE_TIMEOUT)
    if res is None or res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        return None
    stream = None
    for s in data.get("streams") or []:
        if isinstance(s, dict) and s.get("codec_type") == "video":
            stream = s
            break
    if stream is None:
        return None
    duration = _duration_of(data.get("format") or {}, stream)
    if duration <= 0:
        return None  # zero/unknown duration: treat as corrupt
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if _rotation_of(stream) % 180 == 90:
        # Phones store landscape frames + a rotate tag; compare DISPLAY dims.
        width, height = height, width
    return duration, width, height


def _duration_of(fmt: dict, stream: dict) -> float:
    for candidate in (fmt.get("duration"), stream.get("duration")):
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _rotation_of(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list") or []:
        if isinstance(sd, dict) and "rotation" in sd:
            try:
                rot = int(sd["rotation"])
            except (TypeError, ValueError):
                pass
    if not rot:
        try:
            rot = int((stream.get("tags") or {}).get("rotate") or 0)
        except (TypeError, ValueError):
            rot = 0
    return abs(rot) % 360


def remux_to_mp4(src: Path) -> Path:
    """Rewrap a .mov/.m4v as .mp4 (stream copy; re-encode fallback).

    Returns the new .mp4 path and removes the source on success. Raises
    PreflightRejected when neither strategy produces a playable file."""
    src = Path(src)
    out = src.parent / (src.stem + ".mp4")
    res = _run("ffmpeg", [
        "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(out),
    ], timeout=REMUX_TIMEOUT)
    ok = res is not None and res.returncode == 0 and out.exists() and out.stat().st_size > 0
    if not ok:
        # Codec not mp4-compatible (or copy failed): full re-encode.
        res = _run("ffmpeg", [
            "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ], timeout=REENCODE_TIMEOUT)
        ok = res is not None and res.returncode == 0 and out.exists() and out.stat().st_size > 0
    if not ok:
        try:
            out.unlink()
        except OSError:
            pass
        raise PreflightRejected(
            "No pudimos convertir tu video a MP4. Exporta el video como MP4 "
            "(H.264) y vuelve a subirlo."
        )
    try:
        src.unlink()
    except OSError:
        pass
    return out


def _ensure_mp4_name(path: Path) -> Path:
    """Tools unavailable: at least hand the pipeline a .mp4-named file."""
    path = Path(path)
    if path.suffix.lower() == ".mp4":
        return path
    out = path.parent / (path.stem + ".mp4")
    try:
        path.replace(out)
        return out
    except OSError:
        return path


def convert_horizontal_to_vertical(src: Path) -> Path:
    """Auto-crop/convert a horizontal (landscape) video into 9:16 vertical MP4.

    Crops the center 9:16 aspect ratio (crop=ih*9/16:ih) and re-encodes to H.264/AAC.
    """
    src = Path(src)
    out = src.parent / (src.stem + "_vertical.mp4")
    res = _run("ffmpeg", [
        "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(src),
        "-vf", "crop=ih*9/16:ih",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out),
    ], timeout=REENCODE_TIMEOUT)
    ok = res is not None and res.returncode == 0 and out.exists() and out.stat().st_size > 0
    if not ok:
        raise PreflightRejected(
            "No pudimos convertir el video horizontal a vertical. Graba en vertical (9:16) y vuelve a subirlo."
        )
    try:
        if out != src:
            src.unlink()
    except OSError:
        pass
    return out


def prepare_input(tmp: Path, redownload=None, max_seconds: float | None = None) -> Path:
    """Validate a freshly downloaded upload and normalize it to .mp4.

    - Corrupt/unreadable file: one `redownload()` attempt (truncated
      transfers), then PreflightRejected.
    - Longer than MAX_VIDEO_SECONDS: PreflightRejected.
    - Landscape (width > height): Automatically cropped to 9:16 vertical.
    - .mov/.m4v: remuxed to .mp4 (re-encode fallback).

    Returns the path of the validated .mp4 (may differ from `tmp`).
    """
    tmp = Path(tmp)
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        return _ensure_mp4_name(tmp)  # cannot validate; do not block real work
    info = probe(tmp)
    if info is None and redownload is not None:
        try:
            redownload()
        except Exception:
            pass
        info = probe(tmp)
    if info is None:
        raise PreflightRejected(
            "El archivo de video llegó dañado o incompleto y no se puede leer. "
            "Exporta el video de nuevo y vuelve a subirlo."
        )
    duration, width, height = info
    limit = max_seconds if max_seconds is not None else max_video_seconds()
    if duration > limit:
        raise PreflightRejected(
            "El video dura %d segundos y el máximo permitido es %d segundos "
            "(%.0f minutos). Sube un video más corto." % (duration, limit, limit / 60)
        )
    if width and height and width > height:
        tmp = convert_horizontal_to_vertical(tmp)

    if tmp.suffix.lower() != ".mp4":
        return remux_to_mp4(tmp)
    return tmp

