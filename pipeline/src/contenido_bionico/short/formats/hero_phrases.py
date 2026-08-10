"""Dramatic hero-phrase selection for the video-treatment formats.

`hero_phrases(ctx)` asks an agent (opus, medium, tools-less) for VERBATIM
transcript fragments, then GUARDS the result: every phrase is re-checked against
the transcript with `find_verbatim_span` and its times are taken FROM THE
MATCHER, never trusted from the agent. Non-verbatim or wrong-length phrases are
dropped, survivors are spaced apart, and a deterministic fallback (the opening
words of each caption-cue sentence group) takes over when too few survive (weak
agent output, transcript drift).

Two modes serve the two treatments:

- ``mode="dense"`` (default, split-screen band): 8-14 short punchy fragments
  (2-6 words) spread across the whole video, ≥2s apart, sorted by start. Fewer
  than four survivors -> fallback.
- ``mode="cutaway"`` (herotext solid cards): the 3-6 MOST IMPACTFUL statements
  (2-10 words). The agent returns candidates ordered by impact; selection keeps
  the strongest ones whose starts are ≥8s apart (the video must not become
  mostly cards) and clamps each dwell to [1.2s, 4.0s]. Fewer than three
  survivors -> fallback (also fewer + spaced + clamped).

The agent is the ONLY creative decision here; validation/fallback are pure and
unit-tested. Best-effort: any agent failure degrades to the fallback, never
raises.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from contenido_bionico.shared import config
from contenido_bionico.shared.runtime.agent_runner import (
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)
from contenido_bionico.short.formats import adapt, atoms
from contenido_bionico.short.formats.span_map import find_verbatim_span

PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "hero_phrases.md"

AGENT_MODEL = "claude-opus-4-8"
AGENT_EFFORT = "medium"
AGENT_TIMEOUT_S = 300
AGENT_MAX_TURNS = 12

# Phrase constraints (mirrors the agent prompt; the code is the enforcer).
MIN_PHRASE_WORDS = 2
MAX_PHRASE_WORDS = 6
MIN_GAP_S = 2.0        # consecutive hero phrases must start ≥ this far apart
MIN_SURVIVORS = 4      # below this many validated phrases -> deterministic fallback
FALLBACK_HEAD_WORDS = 4  # opening words of a caption cue used as a fallback phrase
FALLBACK_MAX_SEC = 2.5   # cap a fallback phrase's on-screen span

# Cutaway mode (video_herotext solid cards): few, impactful, well-spaced.
CUTAWAY_MAX_WORDS = 10        # a cutaway statement may run longer than a band phrase
CUTAWAY_MIN_GAP_S = 8.0       # card starts must be ≥ this far apart
CUTAWAY_MAX_PHRASES = 6       # never more than this many cards
CUTAWAY_MIN_SURVIVORS = 3     # below this many validated cards -> fallback
CUTAWAY_DWELL_MIN_S = 1.2     # a card always holds at least this long
CUTAWAY_DWELL_MAX_S = 4.0     # ... and never longer than this

# Per-mode knobs. `dwell` is None (span = matcher times, dense) or a (lo, hi)
# clamp applied to the matcher span (cutaway). `by_impact` keeps the agent's
# strongest-first order when spacing conflicts (cutaway) instead of the
# earliest-first greedy of the dense band.
_MODES: dict[str, dict] = {
    "dense": {
        "min_words": MIN_PHRASE_WORDS,
        "max_words": MAX_PHRASE_WORDS,
        "min_gap_s": MIN_GAP_S,
        "max_phrases": 14,
        "min_survivors": MIN_SURVIVORS,
        "dwell": None,
        "by_impact": False,
    },
    "cutaway": {
        "min_words": MIN_PHRASE_WORDS,
        "max_words": CUTAWAY_MAX_WORDS,
        "min_gap_s": CUTAWAY_MIN_GAP_S,
        "max_phrases": CUTAWAY_MAX_PHRASES,
        "min_survivors": CUTAWAY_MIN_SURVIVORS,
        "dwell": (CUTAWAY_DWELL_MIN_S, CUTAWAY_DWELL_MAX_S),
        "by_impact": True,
    },
}


class HeroPhrasesError(RuntimeError):
    pass


def _tagged(name: str, value: str) -> str:
    return f"<{name}>\n{value}\n</{name}>"


def _word_rows(transcript: dict | None) -> list[dict]:
    """Transcript word rows (type=='word'), in order."""
    if not isinstance(transcript, dict):
        return []
    return [w for w in (transcript.get("words") or []) if w.get("type", "word") == "word"]


def _word_text(w: dict) -> str:
    return str(w.get("text") or w.get("word") or "").strip()


def _plain_text(words: list[dict]) -> str:
    return re.sub(r"\s+", " ", " ".join(_word_text(w) for w in words if _word_text(w))).strip()


def _build_message(words: list[dict], mode: str = "dense") -> str:
    rows = "\n".join(
        f"{float(w.get('start', 0.0)):.2f}\t{float(w.get('end', 0.0)):.2f}\t{_word_text(w)}"
        for w in words
        if _word_text(w)
    )
    return "\n".join(
        [
            "Select dramatic hero phrases for this short.",
            "Return only the JSON object on stdout. Do not use tools or read files.",
            _tagged("MODE", mode),
            _tagged("TRANSCRIPT", _plain_text(words)),
            _tagged("WORDS", rows),
        ]
    )


def _parse_json(stdout: str) -> dict:
    """Extract the JSON object the agent returned (tolerant of stray prose)."""
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise HeroPhrasesError(f"hero_phrases agent returned no JSON object: {stdout[:200]!r}")
    try:
        data = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HeroPhrasesError(f"hero_phrases JSON invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise HeroPhrasesError("hero_phrases JSON is not an object")
    return data


def _select_phrases(ctx, words: list[dict], mode: str = "dense") -> list[dict]:
    """Call the agent and return its raw `phrases` list (times untrusted).

    Isolated so tests monkeypatch this single subprocess-spawning step.
    """
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise HeroPhrasesError(missing_agent_cmd_message())
    if not PROMPT_PATH.exists():
        raise HeroPhrasesError(f"hero_phrases system prompt missing: {PROMPT_PATH}")
    config.load_env_into_process()
    log_dir = ctx.run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_name = "hero_phrases.log" if mode == "dense" else f"hero_phrases.{mode}.log"
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=PROMPT_PATH.read_text(encoding="utf-8") + adapt.FIRST_PERSON_VOICE,
        initial_message=_build_message(words, mode),
        cwd=config.REPO_ROOT,
        timeout_seconds=AGENT_TIMEOUT_S,
        max_turns=AGENT_MAX_TURNS,
        tools=[],
        model=AGENT_MODEL,
        effort=AGENT_EFFORT,
        permission_mode=None,
        log_path=log_dir / log_name,
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-400:]
        raise HeroPhrasesError(f"hero_phrases agent failed (exit {result.returncode}); {tail}")
    phrases = _parse_json(result.stdout).get("phrases")
    return phrases if isinstance(phrases, list) else []


def _word_count(text: str) -> int:
    return len([t for t in text.split() if t])


def _thin_by_spacing(phrases: list[dict], *, max_phrases: int, min_gap_s: float) -> list[dict]:
    """Sort by start, then greedily keep phrases ≥ `min_gap_s` apart, capped."""
    kept: list[dict] = []
    for p in sorted(phrases, key=lambda x: (x["start"], x["end"])):
        if kept and p["start"] - kept[-1]["start"] < min_gap_s:
            continue
        if kept and p["text"] == kept[-1]["text"]:
            continue
        kept.append(p)
        if len(kept) >= max_phrases:
            break
    return kept


def _select_by_impact(phrases: list[dict], *, max_phrases: int, min_gap_s: float) -> list[dict]:
    """Keep the strongest phrases that fit the spacing, in the given order.

    `phrases` arrive ordered by impact (strongest first, the cutaway agent
    contract). Walk that order greedily, keeping a phrase only when its start is
    ≥ `min_gap_s` away from EVERY kept start (impact order is not time order, so
    the check is absolute distance, both directions) and its text is new. The
    kept phrases are returned sorted by start for the renderer.
    """
    kept: list[dict] = []
    for p in phrases:
        if any(abs(p["start"] - k["start"]) < min_gap_s for k in kept):
            continue
        if any(p["text"] == k["text"] for k in kept):
            continue
        kept.append(p)
        if len(kept) >= max_phrases:
            break
    return sorted(kept, key=lambda x: (x["start"], x["end"]))


def _clamp_dwell(start: float, end: float, dwell: tuple[float, float] | None) -> float:
    """The phrase's on-screen end, with its span clamped into `dwell` (lo, hi)."""
    if dwell is None:
        return end
    lo, hi = dwell
    return start + min(max(end - start, lo), hi)


def _validate_and_anchor(
    phrases: list[dict], words: list[dict], *, max_phrases: int | None = None, mode: str = "dense"
) -> list[dict]:
    """Keep only verbatim phrases; re-anchor their times from the matcher.

    Drops phrases whose word count is outside the mode's [min,max] words or that
    do not appear verbatim in `words`; times ALWAYS come from
    `find_verbatim_span`, never from the agent. Dense survivors are thinned
    earliest-first to ≥2s apart; cutaway survivors keep the agent's impact order
    under the ≥8s spacing and get their dwell clamped to [1.2s, 4.0s]. Both are
    capped at `max_phrases` (default: the mode's cap).
    """
    cfg = _MODES[mode]
    if max_phrases is None:
        max_phrases = cfg["max_phrases"]
    anchored: list[dict] = []
    for ph in phrases or []:
        text = re.sub(r"\s+", " ", str((ph or {}).get("text") or "").strip())
        if not text:
            continue
        if not (cfg["min_words"] <= _word_count(text) <= cfg["max_words"]):
            continue
        span = find_verbatim_span(text, words)
        if span is None:
            continue
        start = float(span[0])
        end = _clamp_dwell(start, float(span[1]), cfg["dwell"])
        anchored.append({"text": text, "start": round(start, 3), "end": round(end, 3)})
    if cfg["by_impact"]:
        return _select_by_impact(anchored, max_phrases=max_phrases, min_gap_s=cfg["min_gap_s"])
    return _thin_by_spacing(anchored, max_phrases=max_phrases, min_gap_s=cfg["min_gap_s"])


def _fallback_phrases(
    ctx, words: list[dict], *, max_phrases: int | None = None, mode: str = "dense"
) -> list[dict]:
    """Deterministic phrases from the opening words of each caption-cue group.

    For each caption cue, take its first `FALLBACK_HEAD_WORDS` words as a phrase;
    anchor its time from the transcript when that head is itself verbatim,
    otherwise fall back to the cue's own start/end (capped to FALLBACK_MAX_SEC).
    In cutaway mode the result is fewer + spaced like the agent path: dwell
    clamped to [1.2s, 4.0s], starts ≥8s apart, capped at 6.
    """
    cfg = _MODES[mode]
    if max_phrases is None:
        max_phrases = cfg["max_phrases"]
    cues = atoms.caption_cues(ctx.run_dir) or []
    out: list[dict] = []
    for cue in cues:
        toks = [t for t in str(cue.get("text") or "").split() if t]
        if len(toks) < MIN_PHRASE_WORDS:
            continue
        phrase = " ".join(toks[:FALLBACK_HEAD_WORDS])
        span = find_verbatim_span(phrase, words)
        if span is not None:
            start, end = float(span[0]), float(span[1])
        else:
            try:
                start, end = float(cue["start"]), float(cue["end"])
            except (KeyError, TypeError, ValueError):
                continue
        if end <= start:
            end = start + FALLBACK_MAX_SEC
        end = _clamp_dwell(start, min(end, start + FALLBACK_MAX_SEC), cfg["dwell"])
        out.append({"text": phrase, "start": round(start, 3), "end": round(end, 3)})
    return _thin_by_spacing(out, max_phrases=max_phrases, min_gap_s=cfg["min_gap_s"])


def hero_phrases(ctx, *, max_phrases: int | None = None, mode: str = "dense") -> list[dict]:
    """Validated dramatic hero phrases `[{"text","start","end"}]` for a run.

    Agent-selected, verbatim-guarded, spaced per `mode` (see the module
    docstring: "dense" = the 8-14 phrase band, "cutaway" = the 3-6 solid cards).
    Degrades to the caption-cue fallback when too few agent phrases survive
    validation (or the agent is unavailable). Returns [] only when the run has
    no usable transcript. `max_phrases` defaults to the mode's cap.
    """
    cfg = _MODES[mode]
    cap = cfg["max_phrases"] if max_phrases is None else max_phrases
    words = _word_rows(ctx.transcript())
    if not words:
        return []
    try:
        # Dense keeps the historical 2-arg call (tests and older monkeypatches
        # treat `_select_phrases` as (ctx, words)); other modes pass theirs.
        raw = _select_phrases(ctx, words) if mode == "dense" else _select_phrases(ctx, words, mode)
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back on any agent failure
        print(f"[hero] agent no disponible, uso respaldo: {exc}", file=sys.stderr, flush=True)
        raw = []
    validated = _validate_and_anchor(raw, words, max_phrases=cap, mode=mode)
    if len(validated) >= cfg["min_survivors"]:
        return validated
    return _fallback_phrases(ctx, words, max_phrases=cap, mode=mode)
