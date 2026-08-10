"""Per-run atom accessors: read the existing artifacts a finished short leaves
in `runs/<id>/` and hand them to format producers in a stable shape.

Pure reads except `voice_track`, which encodes a cached, loudness-normalized
voice-only track (the one atom that needs ffmpeg). Everything degrades to
`None`/defaults when the atom is absent so the runner can gate on availability.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from contenido_bionico.shared.ffmpeg import (
    NO_WINDOW,
    VOICE_TARGET_LUFS,
    measure_lufs,
    static_gain_db,
)

# Voice-audio source precedence: the CLEAN CUT (`source.mp4`) is the single
# source of truth. Its timeline is exactly what `transcript.json` is aligned to,
# so sourcing the voice from it keeps every voice-based format in sync with the
# transcript's word times AND with the final reel (whose voice is a byte-for-byte
# copy of source.mp4's audio — assembly maps `0:a` unchanged; verified
# cross-correlation 1.0 at 0 ms lag). `final.video_only.mp4` (the assembled
# video's voice-only backup) is only a fallback if the cut source is ever
# missing. `final.mp4` is DELIBERATELY excluded: it is the fully-mixed reel
# (voice + music + SFX) and must never be mistaken for a voice source.
_VOICE_SOURCES = ("source.mp4", "final.video_only.mp4")

# Brand-neutral fallback palette (dark bg, cream paper, dark ink, one accent).
# Used only when a run has no Style_Tokens.json or it omits a key; the comps
# never see a missing palette color.
_DEFAULT_PALETTE = {
    "bg": "#1A1712",
    "ink": "#231D17",
    "paper": "#F6F1E7",
    "accent": "#E8724C",
    "accentSoft": "#F4B69E",
}
_PALETTE_KEYS = ("bg", "ink", "paper", "accent", "accentSoft")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def caption_cues(run_dir: Path) -> list[dict] | None:
    """Caption cues `[{"text","start","end"}]` from `captions_props.json`.

    None when the file is missing/unreadable or carries no cues.
    """
    data = _read_json(run_dir / "captions_props.json")
    if not isinstance(data, dict):
        return None
    cues = data.get("cues")
    if not isinstance(cues, list) or not cues:
        return None
    return cues


def load_carousel_plan(run_dir: Path) -> dict | None:
    """The published carousel plan `{"title","slides":[...]}` or None."""
    data = _read_json(run_dir / "carousel" / "Carousel_Plan.json")
    return data if isinstance(data, dict) else None


def load_quote_plan(run_dir: Path) -> dict | None:
    """The published quote plan `{"quotes":[{"text","fontPx","music"}]}` or None."""
    data = _read_json(run_dir / "quotes" / "Quote_Plan.json")
    return data if isinstance(data, dict) else None


def palette(run_dir: Path) -> dict:
    """Brand palette (5 hex keys). Always returns a full dict.

    Reads `Style_Tokens.json`'s `palette`, overlaying whatever valid string
    values it carries onto the neutral defaults so every key is present.
    """
    pal = dict(_DEFAULT_PALETTE)
    data = _read_json(run_dir / "Style_Tokens.json")
    if isinstance(data, dict):
        src = data.get("palette")
        if isinstance(src, dict):
            for key in _PALETTE_KEYS:
                value = src.get(key)
                if isinstance(value, str) and value.strip():
                    pal[key] = value.strip()
    return pal


def _voice_source(run_dir: Path) -> Path | None:
    for name in _VOICE_SOURCES:
        candidate = run_dir / name
        if candidate.exists():
            return candidate
    return None


def _encode_voice(src: Path, out: Path) -> bool:
    """ffmpeg: strip video, apply a measured static gain to `VOICE_TARGET_LUFS`,
    encode voice-only AAC. Returns True on success.

    Isolated so tests can monkeypatch the single ffmpeg-executing step.
    """
    gain = static_gain_db(measure_lufs(src), VOICE_TARGET_LUFS)
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn", "-af", f"volume={gain}dB",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    return proc.returncode == 0 and out.exists() and out.stat().st_size > 0


def voice_track(run_dir: Path) -> Path | None:
    """Cached, loudness-normalized, voice-only track for this run.

    Encodes `_intermediates/voice_norm.m4a` from the best available voice
    source (see `_VOICE_SOURCES`) once, then reuses it while it is at least as
    new as its source. None when no source exists or encoding fails.
    """
    src = _voice_source(run_dir)
    if src is None:
        return None
    out = run_dir / "_intermediates" / "voice_norm.m4a"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    if not _encode_voice(src, out):
        return None
    return out
