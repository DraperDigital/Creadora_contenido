"""Trim over-long inter-word spacings inside the editor's keep segments.

ElevenLabs Scribe labels the time between two words as a `spacing` token. A long
spacing (e.g. 780 ms) is often NOT clean silence: it can hold a very quiet word
or murmur the speaker said to themselves (re-focusing on the script) that Scribe
does not transcribe and that silence-detection cannot remove -- it is audible,
above the silence threshold. The de-silencer caps only the *silent* parts and
leaves the murmur; the editor/reviewer leave it too (no word detected there).

This helper removes it by POSITION instead of by audio level. For every
inter-word spacing longer than `gap_threshold_s`, it keeps only the first
`keep_head_s` of the spacing (the natural beat right after the preceding word)
and cuts the rest, where the murmur and the extra dead air sit. The murmur is
reliably *after* that head because the de-silencer leaves a capped silence at the
start of the spacing.

It rewrites the editor's keep segments (edl.json) into smaller keep segments
split at those cuts, then `make_render_edl.py` turns them into render ranges.
Runs where the old tightener used to run, but with this single explicit rule.

CLI:
    python trim_spacings.py <edl.json> --words <word_for_word.json> -o <out.json>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_GAP_THRESHOLD_S = 0.200   # only spacings longer than this are trimmed
                                  # (clean pauses sit ~120ms from de-silence; only
                                  # longer ones hide murmurs Scribe missed)
DEFAULT_KEEP_HEAD_S = 0.100       # of a trimmed spacing, keep this much at the start


def load_words(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    words = data.get("words", data) if isinstance(data, dict) else data
    return sorted(
        [w for w in words
         if w.get("type", "word") == "word" and "start" in w and "end" in w],
        key=lambda w: float(w["start"]),
    )


def trim_keep(
    rs: float, re_: float, words: list[dict], gap_s: float, head_s: float
) -> tuple[list[tuple[float, float]], int, float]:
    """Split one keep span [rs, re_] at every inter-word spacing > gap_s, keeping
    only `head_s` at the start of each such spacing. Returns
    (sub_keeps, n_trimmed, removed_seconds)."""
    inside = [w for w in words
              if float(w["start"]) >= rs - 0.01 and float(w["start"]) < re_]
    if len(inside) < 2:
        return [(rs, re_)], 0, 0.0
    keeps: list[tuple[float, float]] = []
    sub_start = rs
    n_trimmed = 0
    removed = 0.0
    for i in range(len(inside) - 1):
        w_end = float(inside[i]["end"])
        nxt_start = float(inside[i + 1]["start"])
        gap = nxt_start - w_end
        if gap > gap_s:
            cut_point = w_end + head_s
            if cut_point < nxt_start:
                keeps.append((sub_start, cut_point))
                removed += nxt_start - cut_point
                n_trimmed += 1
                sub_start = nxt_start
    keeps.append((sub_start, re_))
    return keeps, n_trimmed, removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trim long inter-word spacings by position.")
    ap.add_argument("edl", type=Path, help="editor EDL (edl.json) with segments[]")
    ap.add_argument("--words", type=Path, required=True, help="word_for_word.json")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--gap-threshold-ms", type=float, default=DEFAULT_GAP_THRESHOLD_S * 1000)
    ap.add_argument("--keep-head-ms", type=float, default=DEFAULT_KEEP_HEAD_S * 1000)
    args = ap.parse_args(argv)

    gap_s = args.gap_threshold_ms / 1000.0
    head_s = args.keep_head_ms / 1000.0

    try:
        edl = json.loads(args.edl.read_text(encoding="utf-8"))
        words = load_words(args.words)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read inputs: {exc}", file=sys.stderr)
        return 2

    segments = edl.get("segments", [])
    out_segments: list[dict] = []
    total_trimmed = 0
    total_removed = 0.0
    for s in segments:
        if s.get("status") != "keep":
            out_segments.append(s)
            continue
        rs, re_ = float(s["start"]), float(s["end"])
        subs, n, removed = trim_keep(rs, re_, words, gap_s, head_s)
        total_trimmed += n
        total_removed += removed
        for a, b in subs:
            if b > a:
                out_segments.append({"start": a, "end": b, "status": "keep"})

    out = dict(edl)
    out["segments"] = out_segments
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"trim-spacings: trimmed {total_trimmed} spacing(s) >{args.gap_threshold_ms:.0f}ms "
        f"down to {args.keep_head_ms:.0f}ms (removed {total_removed:.1f}s of murmur/dead air "
        f"that silence-detect and Scribe could not)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
