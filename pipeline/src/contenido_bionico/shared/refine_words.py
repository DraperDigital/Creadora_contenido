"""Refine the editor's keep segments down to WORDS, dropping non-word noise.

The de-silencer (`desilence.py`) caps only *silent* stretches; anything audible
survives — including coughs, throat-clears, lip smacks, chair creaks and the
murmurs a speaker says to themselves. ElevenLabs Scribe does not transcribe those
(no `word` token sits there), and the editor keeps the surrounding span because
it is speech, so the noise rides along into the cut. `trim_spacings.py` removes
audible *inter-word gaps* longer than a threshold, but a noise that sits inside a
short gap, or in a keep segment's lead/tail, still survives.

This step closes that hole: inside every keep segment it finds the regions that
are BOTH audible (above the silence floor) AND not covered by any transcribed
word, and cuts them out. Natural silent micro-pauses are left intact (they are
not audible), so speech rhythm is preserved; only real noise is removed.

It rewrites the EDL (same shape as `trim_spacings.py`'s output) into smaller keep
segments split around the noise. `make_render_edl.py` then turns them into render
ranges and `derive_transcript.py` re-derives the word timings on the new cut, so
no manual re-timing is needed.

CLI:
    python refine_words.py <edl.json> --words <word_for_word.json> \
        --audio <desilenced.mp4> -o <out.json>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contenido_bionico.shared.desilence import (  # noqa: E402
    THRESHOLD_DB,
    _audio_duration,
    detect_silences,
)

# Keep this much audio on EACH side of a word untouched, so a cut never clips a
# word's onset or tail (Scribe's word boundaries are a few tens of ms tight).
WORD_PAD_S = 0.12
# Only remove an audible non-word region at least this long — a real cough/murmur
# is >=~150ms; this avoids nibbling tiny artifacts between tightly-spaced words.
MIN_NOISE_S = 0.15
# Shortest silence the silence map must resolve. The working audio is ALREADY
# de-silenced (long pauses capped to ~120ms), so the default 0.2s detection
# misses every remaining pause and "audible" would span whole keep segments,
# hiding a cough that sits next to a word. Resolving ~60ms pauses isolates each
# audible blob so a real non-word noise stands on its own and gets cut.
SILENCE_MIN_S = 0.06


def load_words(path: Path) -> list[tuple[float, float]]:
    """Word spans [(start, end), ...] from word_for_word.json, sorted."""
    data = json.loads(path.read_text(encoding="utf-8"))
    words = data.get("words", data) if isinstance(data, dict) else data
    spans = [
        (float(w["start"]), float(w["end"]))
        for w in words
        if w.get("type", "word") == "word" and "start" in w and "end" in w
    ]
    return sorted(spans)


def _complement(
    regions: list[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """Sub-intervals of [lo, hi] NOT covered by `regions` (which need not be
    sorted/merged)."""
    out: list[tuple[float, float]] = []
    cursor = lo
    for ra, rb in sorted(regions):
        a = max(ra, lo)
        b = min(rb, hi)
        if b <= a:  # region does not overlap [lo, hi] after clamping
            continue
        if b <= cursor:
            continue
        if a > cursor:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi:
        out.append((cursor, hi))
    return out


def noise_cuts(
    rs: float,
    re_: float,
    silences: list[tuple[float, float]],
    words: list[tuple[float, float]],
    *,
    pad_s: float,
    min_noise_s: float,
) -> list[tuple[float, float]]:
    """Audible, non-word sub-regions of [rs, re_] worth cutting.

    audible = [rs, re_] minus the silence regions; from that subtract every
    word span padded by `pad_s`; what remains and is >= `min_noise_s` is noise.
    """
    audible = _complement(silences, rs, re_)
    padded_words = [(s - pad_s, e + pad_s) for s, e in words]
    cuts: list[tuple[float, float]] = []
    for a, b in audible:
        for ns, ne in _complement(padded_words, a, b):
            if ne - ns >= min_noise_s:
                cuts.append((ns, ne))
    return cuts


def refine_segment(
    rs: float,
    re_: float,
    cuts: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """[rs, re_] minus the cut regions, as kept sub-segments."""
    return _complement(cuts, rs, re_)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cut audible non-word noise from the editor's keep segments."
    )
    ap.add_argument("edl", type=Path, help="EDL (edl_trimmed.json) with segments[]")
    ap.add_argument("--words", type=Path, required=True, help="word_for_word.json")
    ap.add_argument("--audio", type=Path, required=True, help="de-silenced working video/audio")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--word-pad-ms", type=float, default=WORD_PAD_S * 1000)
    ap.add_argument("--min-noise-ms", type=float, default=MIN_NOISE_S * 1000)
    args = ap.parse_args(argv)

    pad_s = args.word_pad_ms / 1000.0
    min_noise_s = args.min_noise_ms / 1000.0

    try:
        edl = json.loads(args.edl.read_text(encoding="utf-8"))
        words = load_words(args.words)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read inputs: {exc}", file=sys.stderr)
        return 2

    if not args.audio.exists():
        print(f"error: audio not found: {args.audio}", file=sys.stderr)
        return 2

    # Audible non-word detection needs a FINE silence map of the (already
    # de-silenced) working audio — resolve short capped pauses so each audible
    # blob is isolated; see SILENCE_MIN_S.
    silences = detect_silences(
        args.audio, threshold_db=THRESHOLD_DB, min_duration_s=SILENCE_MIN_S
    )
    total_dur = _audio_duration(args.audio) or 0.0

    segments = edl.get("segments", [])
    out_segments: list[dict] = []
    n_cuts = 0
    removed = 0.0
    for s in segments:
        if s.get("status") != "keep":
            out_segments.append(s)
            continue
        rs, re_ = float(s["start"]), float(s["end"])
        cuts = noise_cuts(
            rs, re_, silences, words, pad_s=pad_s, min_noise_s=min_noise_s
        )
        n_cuts += len(cuts)
        removed += sum(b - a for a, b in cuts)
        for a, b in refine_segment(rs, re_, cuts):
            if b > a:
                out_segments.append({"start": a, "end": b, "status": "keep"})

    out = dict(edl)
    out["segments"] = out_segments
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"refine-words: removed {n_cuts} audible non-word region(s) "
        f"({removed:.1f}s of cough/murmur/noise) that silence-detect and Scribe "
        f"left in (audio span {total_dur:.1f}s, pad {args.word_pad_ms:.0f}ms, "
        f"min {args.min_noise_ms:.0f}ms)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
