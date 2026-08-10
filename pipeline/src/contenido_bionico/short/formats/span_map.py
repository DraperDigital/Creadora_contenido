"""Map text (quotes, slides) to the transcript time spans where it was spoken,
and slice/join the creator's real voice from those spans.

The deterministic half lives here: `find_verbatim_span` (exact, accent/case/
punctuation-insensitive contiguous match) plus the ffmpeg audio slice/concat
helpers. The agent half (`map_slides_to_spans`, fuzzy slide->idea mapping) is
WS2's job and is stubbed below.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from contenido_bionico.shared.ffmpeg import NO_WINDOW


def _norm(text: str) -> str:
    """Lowercase, strip accents (NFD + drop combining marks) and punctuation.

    'ñ' decomposes to n + combining tilde; dropping the mark folds it to 'n',
    which is the intended, deterministic behavior for matching.
    """
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9ñ ]+", "", text).strip()


def find_verbatim_span(
    quote: str, words: list[dict], *, max_gap_words: int = 2
) -> tuple[float, float] | None:
    """First (start, end) span where `quote` appears verbatim in `words`.

    Matching is contiguous and normalized (accent/case/punctuation-insensitive).
    Returns None when the quote does not appear (paraphrased quotes legitimately
    return None). `words` are transcript word rows `{text|word, start, end, type}`.
    """
    q = [w for w in _norm(quote).split() if w]
    toks = [(_norm(w.get("text") or w.get("word") or ""), w)
            for w in words if w.get("type", "word") == "word"]
    toks = [(n, w) for n, w in toks if n]
    if not q or len(toks) < len(q):
        return None
    for i in range(len(toks) - len(q) + 1):
        if all(toks[i + j][0] == q[j] for j in range(len(q))):
            return (float(toks[i][1]["start"]), float(toks[i + len(q) - 1][1]["end"]))
    return None


def extract_audio_span(voice: Path, start: float, end: float, out: Path) -> Path:
    """Slice `[start, end]` seconds of `voice` into `out` (AAC, 48kHz)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(voice),
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-vn", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, creationflags=NO_WINDOW)
    return out


def concat_audio(parts: list[Path], out: Path, *, gap_s: float = 0.35) -> Path:
    """Concatenate `parts` into `out` with `gap_s` seconds of silence between."""
    if not parts:
        raise ValueError("concat_audio: no parts to concatenate")
    inputs: list[str] = []
    for part in parts:
        inputs += ["-i", str(part)]
    n = len(parts)
    filters: list[str] = []
    labels: list[str] = []
    for i in range(n):
        # Pad every part except the last with the inter-clip gap.
        if i < n - 1 and gap_s > 0:
            filters.append(f"[{i}:a]apad=pad_dur={gap_s:.3f}[a{i}]")
        else:
            filters.append(f"[{i}:a]anull[a{i}]")
        labels.append(f"[a{i}]")
    filter_complex = ";".join(filters) + ";" + "".join(labels) + f"concat=n={n}:v=0:a=1[out]"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex, "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, creationflags=NO_WINDOW)
    return out


# --- WS2: slide -> voice-span mapping (agent-driven, deterministic fallback) ---

# Cheap mapper: sonnet at low effort, tools-less, one JSON object out.
_SPANS_MODEL = "claude-sonnet-5"
_SPANS_EFFORT = "low"
_SPANS_TIMEOUT_S = 180
_SPANS_MAX_TURNS = 6
_SPANS_PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "slide_spans.md"

# A voice span shorter than this reads as a flash of audio; widen up to it.
_MIN_SPAN_S = 1.2


def _word_rows(transcript: dict | None) -> list[dict]:
    """Spoken word rows (type == 'word') from a transcript payload, in order."""
    if not isinstance(transcript, dict):
        return []
    words = transcript.get("words") or []
    rows: list[dict] = []
    for w in words:
        if not isinstance(w, dict) or w.get("type", "word") != "word":
            continue
        try:
            start = float(w["start"])
            end = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(w.get("text") or w.get("word") or "").strip()
        if not text:
            continue
        rows.append({"text": text, "start": start, "end": end})
    return rows


def _transcript_duration(words: list[dict]) -> float:
    """Timeline length = the last spoken word's end (0.0 when empty)."""
    return max((w["end"] for w in words), default=0.0)


def _proportional_spans(n: int, duration: float) -> list[tuple[float, float]]:
    """Equal split of `[0, duration]` into `n` back-to-back spans (the fallback)."""
    if n <= 0 or duration <= 0:
        return []
    step = duration / n
    return [(round(i * step, 3), round((i + 1) * step, 3)) for i in range(n)]


def _validate_spans(
    raw: object, n: int, duration: float, *, min_len: float = _MIN_SPAN_S
) -> list[tuple[float, float]] | None:
    """Coerce agent-returned spans into a clean per-slide span list, or None.

    Enforces the contract: exactly `n` numeric spans, clamped to `[0, duration]`,
    each widened symmetrically to at least `min_len` (best-effort — capped by the
    timeline), starts non-decreasing, and every `end > start`. Any structural
    violation returns None so the caller falls back to a proportional split.
    """
    if not isinstance(raw, list) or len(raw) != n or duration <= 0:
        return None
    spans: list[list[float]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            return None
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        # Widen anything under min_len symmetrically, then re-clamp into range.
        length = end - start
        if length < min_len:
            grow = (min_len - length) / 2.0
            start -= grow
            end += grow
            if start < 0.0:
                end -= start
                start = 0.0
            if end > duration:
                start -= end - duration
                end = duration
            start = max(0.0, start)
            end = min(duration, end)
        spans.append([start, end])
    # Starts must move forward (the slides tell the story in order).
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][0] - 1e-6:
            return None
    # Final sanity: a real, non-empty span for every slide.
    for start, end in spans:
        if end <= start:
            return None
    return [(round(s, 3), round(e, 3)) for s, e in spans]


def _slides_block(slides: list[str]) -> str:
    return "\n".join(f"{i}) {str(s).strip()}" for i, s in enumerate(slides, start=1))


def _transcript_block(words: list[dict]) -> str:
    return "\n".join(f"{w['start']:.2f}\t{w['end']:.2f}\t{w['text']}" for w in words)


def _agent_spans(
    video_id: str | int, slides: list[str], words: list[dict], duration: float
) -> object:
    """Call the slide-spans agent and return its parsed `spans` list.

    Raises on any failure (missing CLI, agent error, unparseable output). The
    public entry point catches everything and falls back deterministically.
    """
    # Imported lazily so the deterministic half of this module (and its tests)
    # never pull in the agent runtime.
    from contenido_bionico.shared import config
    from contenido_bionico.shared.runtime.agent_runner import (
        missing_agent_cmd_message,
        resolve_agent_cmd,
        run_agent_code,
    )

    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise RuntimeError(missing_agent_cmd_message())
    if not _SPANS_PROMPT_PATH.exists():
        raise RuntimeError(f"slide_spans system prompt missing: {_SPANS_PROMPT_PATH}")

    config.load_env_into_process()
    run_dir = config.REPO_ROOT / "runs" / str(video_id)
    log_dir = run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)

    message = "\n".join(
        [
            "Map each slide to its voice span in the transcript.",
            "Return only the JSON object on stdout. Do not use tools or read files.",
            f"<DURACION>\n{duration:.2f}\n</DURACION>",
            f"<SLIDES>\n{_slides_block(slides)}\n</SLIDES>",
            f"<TRANSCRIPT>\n{_transcript_block(words)}\n</TRANSCRIPT>",
        ]
    )
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=_SPANS_PROMPT_PATH.read_text(encoding="utf-8"),
        initial_message=message,
        cwd=config.REPO_ROOT,
        timeout_seconds=_SPANS_TIMEOUT_S,
        max_turns=_SPANS_MAX_TURNS,
        tools=[],
        model=_SPANS_MODEL,
        effort=_SPANS_EFFORT,
        permission_mode=None,
        log_path=log_dir / "slide_spans.log",
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-400:]
        raise RuntimeError(f"slide_spans agent failed (exit {result.returncode}); {tail}")

    stdout = result.stdout
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"slide_spans returned no JSON object: {stdout[:200]!r}")
    data = json.loads(stdout[start : end + 1])
    if not isinstance(data, dict):
        raise RuntimeError("slide_spans JSON is not an object")
    spans = data.get("spans")
    if not isinstance(spans, list):
        raise RuntimeError("slide_spans JSON has no 'spans' array")
    # Reorder by the 1-based slide index when present so ordering never depends
    # on the agent emitting the array in order.
    if all(isinstance(s, dict) and "i" in s for s in spans):
        try:
            spans = sorted(spans, key=lambda s: int(s["i"]))
        except (TypeError, ValueError):
            pass
    return spans


def map_slides_to_spans(
    slides: list[str], transcript: dict, video_id: str | int
) -> list[tuple[float, float]]:
    """Map each slide text to the transcript span where its idea is discussed.

    Asks the slide-spans agent for one `[start, end]` per slide, validates the
    result against the transcript timeline, and returns it. ANY failure (no
    agent, agent error, bad/short/out-of-order spans) degrades to a deterministic
    PROPORTIONAL fallback: an equal split of `[0, duration]` into `len(slides)`
    spans. Never raises.
    """
    words = _word_rows(transcript)
    duration = _transcript_duration(words)
    n = len(slides)
    if n == 0 or duration <= 0:
        return _proportional_spans(n, duration)
    try:
        validated = _validate_spans(_agent_spans(video_id, slides, words, duration), n, duration)
        if validated is None:
            raise ValueError("slide spans failed validation")
        return validated
    except Exception as exc:  # noqa: BLE001 — best-effort: always yield spans
        print(
            f"[formats] map_slides_to_spans fallback ({type(exc).__name__}: {exc})",
            file=sys.stderr,
            flush=True,
        )
        return _proportional_spans(n, duration)
