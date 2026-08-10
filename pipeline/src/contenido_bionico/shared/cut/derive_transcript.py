"""Derive a transcript.json from already-produced cut artifacts.

Avoids a second ElevenLabs call after the cut. The cut is fully deterministic
once the editor approves a transcript: every word in `word_for_word.json`
either falls inside a keep-range from `edl_render.json` (and survives into
`source.mp4`) or it doesn't (and is removed). For each surviving word we just
subtract the cumulative skipped duration to translate its raw-time stamp into
cut-time.

Usage:
    python derive_transcript.py <word_for_word.json> <edl_render.json> -o <transcript.json>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def derive(word_for_word_path: Path, edl_render_path: Path) -> dict:
    raw = json.loads(word_for_word_path.read_text(encoding="utf-8-sig"))
    edl = json.loads(edl_render_path.read_text(encoding="utf-8-sig"))

    ranges = sorted(edl.get("ranges", []), key=lambda r: float(r["start"]))
    if not ranges:
        raise ValueError(f"no keep ranges in {edl_render_path}")

    # Precompute cumulative offsets so we can map raw_time -> cut_time in one pass.
    # cum_before[i] = sum of (end-start) of all ranges with index < i
    cum_before = []
    running = 0.0
    for r in ranges:
        cum_before.append(running)
        running += float(r["end"]) - float(r["start"])

    def raw_to_cut(t: float) -> float | None:
        for i, r in enumerate(ranges):
            r_start = float(r["start"])
            r_end = float(r["end"])
            if r_start <= t <= r_end:
                return cum_before[i] + (t - r_start)
        return None

    def on_range_boundary(t: float, eps: float = 1e-6) -> bool:
        return any(
            abs(t - float(r["start"])) <= eps or abs(t - float(r["end"])) <= eps
            for r in ranges
        )

    out_words = []
    for w in raw.get("words", []):
        try:
            ws = float(w["start"])
            we = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        cut_start = raw_to_cut(ws)
        cut_end = raw_to_cut(we)
        if cut_start is None or cut_end is None:
            continue
        if we <= ws and on_range_boundary(ws):
            # Scribe collapses retake clusters into zero-duration words, and the
            # mapper places its cut exactly on that instant. Such a word carries
            # no audio in the cut, but the inclusive range test above would keep
            # it, so the transcript would claim words source.mp4 never says and
            # the cut contract validator would reject the whole cut.
            continue
        if cut_end - cut_start <= 1e-6:
            # The word's raw span survives only as a single instant in the cut
            # (e.g. its edges map to the same cut time across a removed
            # region). A zero-duration word carries no audio either, and such
            # phantom words desync verify_contract's ordered walk over the cut.
            continue
        new_word = dict(w)
        new_word["start"] = round(cut_start, 3)
        new_word["end"] = round(cut_end, 3)
        out_words.append(new_word)

    text = "".join(w.get("text", "") for w in out_words)

    return {
        "language_code": raw.get("language_code", ""),
        "language_probability": raw.get("language_probability", 1.0),
        "text": text,
        "words": out_words,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("word_for_word", type=Path)
    p.add_argument("edl_render", type=Path)
    p.add_argument("-o", "--output", type=Path, required=True)
    args = p.parse_args()

    try:
        result = derive(args.word_for_word, args.edl_render)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({len(result['words'])} words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
