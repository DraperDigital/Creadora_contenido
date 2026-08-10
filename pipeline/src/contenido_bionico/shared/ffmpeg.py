"""Shared ffmpeg/ffprobe subprocess helpers.

Single home for the Windows no-console-window creationflags constant, thin
``run_ffmpeg``/``run_ffprobe`` wrappers, the duration/dimension probes, and
the common H.264 output parameter set — all of which used to be duplicated
per module across the pipelines.

Two duration contracts exist on purpose (callers differ):

  - ``probe_duration``        raises on any failure (CalledProcessError /
                              OSError / ValueError), exactly like the local
                              check_output helpers it replaces.
  - ``probe_duration_or_none`` best-effort: returns ``None`` on ANY failure.

This module imports nothing from the rest of the package, so it is safe to
import from anywhere (including the standalone helper scripts that run by
path as child processes).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Sequence

# CREATE_NO_WINDOW: keep ffmpeg/ffprobe (and other helper subprocesses) from
# flashing a console window on Windows. 0 (no-op) everywhere else.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Common H.264 video-encode parameter set shared by the assembly renderers
# (video args only; call sites append their own audio/container/output args).
X264_BASELINE = [
    "-c:v", "libx264",
    "-preset", "slow",
    # CRF 18 = visually lossless. (Was 1 -- near-lossless, ~140 Mbps, which
    # bloated a 95s short to ~1.7 GB. 18 is ~10x smaller with no visible loss.)
    "-crf", "18",
    "-pix_fmt", "yuv420p",
]

# Loudness policy (EBU R128 integrated LUFS), shared by the TTS overlay and the
# audio mix so the level rules live in ONE place. The voice (creator speech AND
# the synthetic TTS, both normalized to the SAME target) sits at
# VOICE_TARGET_LUFS so the output level is consistent regardless of mic, room,
# or speaker; background music sits MUSIC_OFFSET_LU below the voice so it never
# competes with speech; SFX sit SFX_OFFSET_LU below the voice so accents read
# without startling. The mixed master is nudged from the voice level toward
# MASTER_TARGET_LUFS (the platform-normalization sweet spot) and capped by a
# limiter at MASTER_PEAK_LIMIT (-1 dBTP as a linear amplitude, 10^(-1/20)).
#
# Normalization is done as a MEASURED STATIC GAIN (measure_lufs -> a single
# `volume=NdB`), NOT ffmpeg's `loudnorm` filter. Single-pass `loudnorm` is a
# DYNAMIC normalizer: its gain ramps over the first ~minute (the level audibly
# creeps up), which is wrong for a talking-head take. A constant gain raises or
# lowers the WHOLE track by one fixed amount — same average level, no pumping.
VOICE_TARGET_LUFS = -16.0
# Music sits below the voice but must stay clearly AUDIBLE under a talking head.
# A -20 LU offset (music at -36 LUFS) plus the sidechain duck made the bed
# inaudible during near-continuous speech; -12 LU (music at -28 LUFS, ~12 LU
# under dialogue) is a normal, present-but-under social mix. Tune with _DUCK_FILTER.
MUSIC_OFFSET_LU = -12.0
MUSIC_TARGET_LUFS = VOICE_TARGET_LUFS + MUSIC_OFFSET_LU  # -28.0 LUFS
SFX_OFFSET_LU = -6.0
SFX_TARGET_LUFS = VOICE_TARGET_LUFS + SFX_OFFSET_LU  # -22.0 LUFS
MASTER_TARGET_LUFS = -14.0
MASTER_PEAK_LIMIT = 0.891  # alimiter ceiling, -1 dBTP expressed linearly


def _run_tool(
    binary: str,
    args: Sequence[str],
    *,
    timeout: float | None = None,
    **run_kwargs: object,
) -> subprocess.CompletedProcess:
    """subprocess.run a media tool with NO_WINDOW applied.

    `args` is the argv AFTER the binary name. capture_output/text default to
    True (override via kwargs). Does NOT raise on a non-zero exit unless
    `check=True` is passed — callers keep their own error contracts. A missing
    binary raises FileNotFoundError with an actionable Spanish message.
    """
    run_kwargs.setdefault("capture_output", True)
    run_kwargs.setdefault("text", True)
    try:
        return subprocess.run(  # noqa: PLW1510 (check is caller's contract)
            [binary, *[str(a) for a in args]],
            timeout=timeout,
            creationflags=NO_WINDOW,
            **run_kwargs,  # type: ignore[arg-type]
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"No encuentro '{binary}' en el PATH. Instala ffmpeg "
            "(incluye ffprobe) y vuelve a intentarlo."
        ) from exc


def run_ffmpeg(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    **run_kwargs: object,
) -> subprocess.CompletedProcess:
    """Run ffmpeg with `args` (argv after the binary). See `_run_tool`."""
    return _run_tool("ffmpeg", args, timeout=timeout, **run_kwargs)


def run_ffprobe(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    **run_kwargs: object,
) -> subprocess.CompletedProcess:
    """Run ffprobe with `args` (argv after the binary). See `_run_tool`."""
    return _run_tool("ffprobe", args, timeout=timeout, **run_kwargs)


def measure_lufs(media_path: Path | str) -> float | None:
    """Integrated loudness (LUFS) of a media file's first audio stream.

    Runs ffmpeg's `loudnorm` in ANALYSIS-only mode (`print_format=json`, output
    discarded to the null muxer) and parses `input_i` from the JSON it prints to
    stderr. Returns None when the file has no measurable audio (near-silent
    inputs report values <= -70 LUFS, treated as unmeasurable). This is a pure
    measurement — it never alters the audio; the caller applies a static gain.
    """
    res = _run_tool(
        "ffmpeg",
        [
            "-hide_banner", "-nostats",
            "-i", str(media_path),
            "-vn",
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ],
    )
    err = res.stderr or ""
    start = err.rfind("{")
    end = err.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = float(json.loads(err[start : end + 1]).get("input_i"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if value <= -70.0:
        return None
    return value


def static_gain_db(
    measured_lufs: float | None,
    target_lufs: float,
    *,
    max_boost_db: float = 24.0,
    max_cut_db: float = 24.0,
) -> float:
    """Constant dB gain that brings `measured_lufs` to `target_lufs`, clamped.

    Returns 0.0 when the measurement is unavailable (leave the level untouched).
    The clamp keeps a pathological measurement (e.g. a near-silent take) from
    asking for an absurd boost that would only amplify noise.
    """
    if measured_lufs is None:
        return 0.0
    gain = target_lufs - measured_lufs
    return round(max(-max_cut_db, min(max_boost_db, gain)), 2)


_DURATION_ARGS = [
    "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=nw=1:nk=1",
]


def probe_duration(path: Path | str) -> float:
    """Media duration in seconds via ffprobe; raises on failure.

    Same contract as the per-module check_output helpers it replaces:
    subprocess.CalledProcessError on a non-zero ffprobe exit, OSError when
    ffprobe is missing, ValueError when the output is not numeric. stdout is
    captured; stderr passes through to the parent process like before.
    """
    out = subprocess.check_output(
        ["ffprobe", *_DURATION_ARGS, str(path)],
        text=True,
        creationflags=NO_WINDOW,
    )
    return float(out.strip())


def probe_duration_or_none(
    path: Path | str,
    *,
    timeout: float | None = None,
) -> float | None:
    """Best-effort media duration in seconds; None when ffprobe can't say."""
    try:
        proc = run_ffprobe([*_DURATION_ARGS, str(path)], timeout=timeout)
        if proc.returncode != 0:
            return None
        return float(proc.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def probe_dimensions(path: Path | str) -> tuple[int, int]:
    """(width, height) of the first video stream; raises RuntimeError."""
    proc = run_ffprobe(
        [
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe could not read video dimensions for {path}: {proc.stderr.strip()}"
        )
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError(
            f"ffprobe found no video stream in {path} (audio-only file?): {proc.stderr.strip()}"
        )
    first = lines[0].strip()
    try:
        width_s, height_s = first.split("x", 1)
        return int(width_s), int(height_s)
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"ffprobe returned invalid dimensions for {path}: {first!r}") from exc


def strip_rotation_metadata(path: Path | str) -> None:
    """Remove a stale display-matrix rotation tag from a video whose pixels are
    ALREADY upright.

    Phone clips are stored as a landscape raster plus a -90 "rotate me" display
    matrix. When ffmpeg re-encodes one through a MULTI-SEGMENT concat filter
    graph (the cut, the de-silencer) it auto-rotates the PIXELS upright to
    1080x1920 but ALSO copies that -90 matrix onto the output, so a player
    rotates the already-upright frame a second time (sideways) and
    ``probe_dimensions`` chokes on the side-data ffprobe then appends. A
    ``-display_rotation 0`` stream-copy remux rewrites the container with no
    rotation and leaves the pixels untouched.

    Best-effort and idempotent: a clip with no rotation tag is left untouched,
    and any ffmpeg failure leaves the original file in place.
    """
    src = Path(path)
    rot = run_ffprobe(
        [
            "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream_side_data=rotation",
            "-of", "default=nw=1:nk=1", str(src),
        ],
        encoding="utf-8", errors="replace",
    )
    if not (rot.stdout or "").strip():
        return  # no rotation tag: pixels already display upright, nothing to do
    tmp = src.with_name(f".{src.stem}.derot{src.suffix}")
    proc = run_ffmpeg(
        [
            "-y", "-loglevel", "error",
            "-display_rotation", "0", "-i", str(src),
            "-map", "0", "-c", "copy", "-movflags", "+faststart", str(tmp),
        ]
    )
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        os.replace(tmp, src)
        return
    try:
        tmp.unlink()
    except OSError:
        pass
