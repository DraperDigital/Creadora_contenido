"""Quote builder: finished short run -> standalone quote-post mp4s.

Two render stages per quote:
  (A) a LOSSLESS still PNG via Remotion (the `quote-post` composition), and
  (B) ffmpeg loops it into a 10s mp4 with music.

The quote-post is the brand TWEET CARD (QuotePost.tsx): the committed
shared/quote-template/quote_layout.png (photo, name, handle, verified badge) with
only the body quote rendered live, in a system sans — so it needs the template
image but NO bundled font file.

Steps:
  1. The `quote_select` agent reads the proofread transcript and returns the
     quote strings (just words, no styling).
  2. `_fit_quote_font_px` picks the largest font size that fits each quote in the
     896x850 text box, using a heuristic glyph-width estimate (no font file).
  3. Each quote is rendered to a PNG, then `make_quote_video` builds the 10s mp4.

Output: `runs/<id>/quotes/quote_1.mp4 ...`. Best-effort and re-runnable.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contenido_bionico.shared import config  # noqa: E402
from contenido_bionico.shared.ffmpeg import (  # noqa: E402
    NO_WINDOW,
    VOICE_TARGET_LUFS,
    measure_lufs,
    static_gain_db,
)
from contenido_bionico.shared.remotion_manifest_quote import (  # noqa: E402
    write_quote_post_compositions_ts,
)
from contenido_bionico.shared.render_remotion import (  # noqa: E402
    RemotionRenderError,
    render_isolated_still,
)
from contenido_bionico.shared.runtime.agent_runner import (  # noqa: E402
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)

RUNS_DIR = REPO_ROOT / "runs"
PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "quote_select.md"
SHORTS_MUSIC_DIR = REPO_ROOT / "audio_library" / "music" / "shorts"
# Brand tweet-card template (photo, name, handle, verified badge baked in). The
# committed source lives under shared/quote-template/; it is staged into the
# Remotion public assets so QuotePost's staticFile("assets/quote/layout.png")
# resolves. No font file is needed (QuotePost renders the body in a system sans).
QUOTE_ASSETS = SRC_ROOT / "contenido_bionico" / "shared" / "remotion" / "public" / "assets" / "quote"
TEMPLATE_SRC = SRC_ROOT / "contenido_bionico" / "shared" / "quote-template" / "quote_layout.png"
TEMPLATE_DEST = QUOTE_ASSETS / "layout.png"

AGENT_MODEL = "claude-opus-4-8"
AGENT_EFFORT = "medium"
AGENT_TIMEOUT_S = 300
AGENT_MAX_TURNS = 12

# Text box the quote must fit in (matches QuotePost.tsx: 896px column, 850px tall).
BOX_W = 896
BOX_H = 850
LINE_HEIGHT = 1.16
FONT_MAX_PX = 96
FONT_MIN_PX = 40
FONT_STEP = 2
# Average glyph advance for a bold sans-serif (the system face QuotePost renders
# with), as a fraction of the font size. A deliberate slight over-estimate so the
# heuristic fit never overflows the column. Mirrors captions._CHAR_WIDTH_RATIO.
_CHAR_WIDTH_RATIO = 0.60


class QuoteError(RuntimeError):
    pass


# --- Font fitting (heuristic; no external font file required) ------------------


def _text_width(text: str, px: float) -> float:
    """Approximate pixel width of `text` at `px` for a bold sans-serif."""
    return len(text) * px * _CHAR_WIDTH_RATIO


def _wrap_lines(text: str, px: float, max_w: float) -> list[str]:
    """Greedy word-wrap `text` so each line is <= `max_w` px at `px`."""
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        trial = word if not current else f"{current} {word}"
        if current and _text_width(trial, px) > max_w:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _fit_quote_font_px(text: str) -> int:
    """Largest font px (FONT_MAX..FONT_MIN, step 2) at which `text` fits the box.

    For each candidate size, greedy-wrap to <= BOX_W and accept when
    lines x px x LINE_HEIGHT <= BOX_H. First (largest) that fits wins.
    """
    for px in range(FONT_MAX_PX, FONT_MIN_PX - 1, -FONT_STEP):
        lines = _wrap_lines(text, px, BOX_W)
        if len(lines) * px * LINE_HEIGHT <= BOX_H:
            return px
    return FONT_MIN_PX


# --- ffmpeg (still -> 10s mp4 with music) -------------------------------------


def make_quote_video(png: Path, music: Path, out_mp4: Path) -> None:
    """Loop the still into a 10s mp4 with a faded, loudness-normalized music bed.

    The still is looped for 10s @ 30fps; the music is trimmed to 10s, faded
    0.6s in / 0.6s out, and brought to -16 LUFS (VOICE_TARGET_LUFS) since it is
    the only audio. Normalization is a measured STATIC gain (measure_lufs ->
    one `volume=NdB`), not single-pass `loudnorm`, whose dynamic gain ramp
    audibly creeps over the clip; an alimiter caps peaks after any boost
    (same pattern as the TTS overlay).
    """
    if out_mp4.exists():
        out_mp4.unlink()
    music_gain = static_gain_db(measure_lufs(music), VOICE_TARGET_LUFS)
    audio_filter = (
        "[1:a]atrim=duration=10,asetpts=N/SR/TB,"
        "afade=t=in:st=0:d=0.6,afade=t=out:st=9.4:d=0.6,"
        f"volume={music_gain}dB,alimiter=limit=0.95[a]"
    )
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", "30", "-t", "10", "-i", str(png),
        "-i", str(music),
        "-filter_complex", audio_filter,
        "-map", "0:v", "-map", "[a]", "-t", "10", "-r", "30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out_mp4),
    ]
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=NO_WINDOW,
    )
    if proc.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size == 0:
        raise QuoteError(
            f"ffmpeg failed building {out_mp4.name} (exit {proc.returncode}): "
            f"{(proc.stderr or '')[-400:]}"
        )


# --- Agent (quote selection) --------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _transcript_text(transcript: dict[str, Any]) -> str:
    words = transcript.get("words") or []
    parts = [
        str(w.get("text") or w.get("word") or "").strip()
        for w in words
        if w.get("type") == "word"
    ]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _call_agent(run_dir: Path, message: str) -> str:
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise QuoteError(missing_agent_cmd_message())
    if not PROMPT_PATH.exists():
        raise QuoteError(f"quote system prompt missing: {PROMPT_PATH}")
    log_dir = run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=PROMPT_PATH.read_text(encoding="utf-8"),
        initial_message=message,
        cwd=REPO_ROOT,
        timeout_seconds=AGENT_TIMEOUT_S,
        max_turns=AGENT_MAX_TURNS,
        tools=[],
        model=AGENT_MODEL,
        effort=AGENT_EFFORT,
        permission_mode=None,
        log_path=log_dir / "quote_select.log",
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-400:]
        raise QuoteError(f"quote_select agent failed (exit {result.returncode}); {tail}")
    return result.stdout


def _parse_quotes(stdout: str) -> list[str]:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise QuoteError(f"quote_select returned no JSON object: {stdout[:200]!r}")
    try:
        data = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError as exc:
        raise QuoteError(f"quote_select JSON invalid: {exc}") from exc
    out: list[str] = []
    for item in data.get("quotes") or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = re.sub(r"\s+", " ", str(text or "").strip())
        if text:
            out.append(text)
    return out


def _stage_template() -> None:
    """Copy the brand quote template into the Remotion public assets so the
    `quote-post` composition's staticFile("assets/quote/layout.png") resolves.
    Raises if the committed template is missing (quotes can't render without it;
    the pipeline swallows this so the video itself is unaffected)."""
    if not TEMPLATE_SRC.exists():
        raise QuoteError(
            f"quote template missing: {TEMPLATE_SRC} "
            "(add quote_layout.png under shared/quote-template/)"
        )
    TEMPLATE_DEST.parent.mkdir(parents=True, exist_ok=True)
    if (
        not TEMPLATE_DEST.exists()
        or TEMPLATE_DEST.stat().st_size != TEMPLATE_SRC.stat().st_size
    ):
        TEMPLATE_DEST.write_bytes(TEMPLATE_SRC.read_bytes())


def _music_tracks() -> list[Path]:
    if not SHORTS_MUSIC_DIR.is_dir():
        return []
    return sorted(
        p for p in SHORTS_MUSIC_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac")
    )


def generate_quotes(video_id: str | int, notes: str | None = None) -> list[Path]:
    """Build and render the quote posts for a finished short run.

    Returns the list of rendered mp4 paths under `runs/<id>/quotes/`. Always
    returns at least one quote or raises QuoteError.
    """
    run_dir = RUNS_DIR / str(video_id)
    transcript_path = run_dir / "transcript.json"
    if not transcript_path.exists():
        raise QuoteError(f"transcript.json not found under {run_dir}")
    _stage_template()
    config.load_env_into_process()

    transcript = _load_json(transcript_path)
    text = _transcript_text(transcript)
    if not text:
        raise QuoteError("transcript has no spoken text")

    parts = [
        "Run the Quote Select job.",
        "Return only the JSON object on stdout. Do not use tools or read files.",
        f"<TRANSCRIPT>\n{text}\n</TRANSCRIPT>",
    ]
    if notes:
        parts.append(f"<USER_CHANGE_REQUEST>\n{notes}\n</USER_CHANGE_REQUEST>")
        parts.append(
            "A previous set of quote posts was already produced from this transcript. "
            "The user requested the changes above. Apply them; otherwise keep your "
            "normal behavior."
        )
    message = "\n".join(parts)
    print(f"[quote] {video_id}: selecting quote-worthy ideas via quote_select", flush=True)
    quotes = _parse_quotes(_call_agent(run_dir, message))
    if not quotes:
        print(f"[quote] {video_id}: empty selection; retrying with an explicit floor", flush=True)
        retry_message = message + "\n" + (
            "Your previous answer returned an empty list. That is INVALID: you must "
            "return AT LEAST ONE quote. Pick the single strongest self-contained idea "
            "in the transcript and return it as the only quote."
        )
        quotes = _parse_quotes(_call_agent(run_dir, retry_message))
    if not quotes:
        raise QuoteError("quote_select returned no quotes after retry")

    out_dir = run_dir / "quotes"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in (*out_dir.glob("quote_*.png"), *out_dir.glob("quote_*.mp4")):
        stale.unlink()

    tracks = _music_tracks()
    if not tracks:
        raise QuoteError(f"no music tracks in {SHORTS_MUSIC_DIR}")

    print(f"[quote] {video_id}: rendering {len(quotes)} quote post(s)", flush=True)
    mp4s: list[Path] = []
    plan: list[dict[str, Any]] = []
    for i, quote in enumerate(quotes, start=1):
        font_px = _fit_quote_font_px(quote)
        png = out_dir / f"quote_{i}.png"
        try:
            render_isolated_still(
                write_compositions=(
                    lambda p, q=quote, fp=font_px: write_quote_post_compositions_ts(
                        text=q, font_px=fp, out_path=p
                    )
                ),
                composition_id="quote-post",
                out_png=png,
            )
        except RemotionRenderError as exc:
            raise QuoteError(f"quote render failed: {exc}") from exc
        music = random.choice(tracks)
        mp4 = out_dir / f"quote_{i}.mp4"
        make_quote_video(png, music, mp4)
        mp4s.append(mp4)
        plan.append({"text": quote, "fontPx": font_px, "music": music.name})

    (out_dir / "Quote_Plan.json").write_text(
        json.dumps({"quotes": plan}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[quote] {video_id}: done -> {len(mp4s)} quote post(s)", flush=True)
    return mp4s


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render quote posts from a short run.")
    parser.add_argument("video_id", help="run id, e.g. 1_short")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args(argv)
    try:
        mp4s = generate_quotes(args.video_id, notes=args.notes)
    except QuoteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {len(mp4s)} quote post(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
