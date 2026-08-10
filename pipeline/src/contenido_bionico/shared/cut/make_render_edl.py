"""Convert `edl_trimmed.json` (list of segments with status keep/cut) into
`edl_render.json` (list of keep ranges) ready for `render_edl.py`.

This conversion used to be done by hand by the operator in earlier runs. It is
formalized here so the cut orchestrator can execute it deterministically.

Usage:
    python make_render_edl.py <edl_trimmed.json> -o <edl_render.json>

Input shape (edl_trimmed.json):
    {
      "source": "...",
      "segments": [
        {"start": 0.0, "end": 5.2, "status": "keep"},
        {"start": 5.2, "end": 5.8, "status": "cut", "text": "[silence]"},
        ...
      ]
    }

Output shape (edl_render.json):
    {
      "source": "...",
      "ranges": [
        {"source": "...", "start": 0.0, "end": 5.2},
        ...
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _has_words(text: object) -> bool:
    """True when a segment's text is actual speech, not a silence marker.

    Silence segments carry an empty/whitespace text or a bracketed marker like
    ``[silence]`` / ``[pause]``. Everything else is spoken words.
    """
    t = str(text or "").strip()
    if not t:
        return False
    return not (t.startswith("[") and t.endswith("]"))


def _word_cut_ranges(segments: list[dict], source: str) -> list[dict]:
    """Keep-silences ranges: the whole span MINUS the spoken-word cuts.

    The editorial cut removes only WORDS, never silences (deliberate pauses are
    the TTS slots and are trimmed later, after TTS, by the de-silencer). So the
    render keeps everything from the first to the last kept word EXCEPT the spans
    of cut segments that contain actual speech — silence gaps and silence-tagged
    cuts are preserved.
    """
    keeps = [
        (float(s["start"]), float(s["end"]))
        for s in segments
        if s.get("status") == "keep" and float(s["end"]) > float(s["start"])
    ]
    if not keeps:
        return []
    span_start = min(s for s, _ in keeps)
    span_end = max(e for _, e in keeps)

    cuts = sorted(
        (max(span_start, float(s["start"])), min(span_end, float(s["end"])))
        for s in segments
        if s.get("status") == "cut"
        and _has_words(s.get("text"))
        and float(s["end"]) > span_start
        and float(s["start"]) < span_end
    )
    # Merge overlapping/adjacent word-cuts so the complement is clean.
    merged: list[tuple[float, float]] = []
    for cs, ce in cuts:
        if merged and cs <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ce))
        else:
            merged.append((cs, ce))

    ranges: list[dict] = []
    cursor = span_start
    for cs, ce in merged:
        if cs > cursor:
            ranges.append({"source": source, "start": cursor, "end": cs})
        cursor = max(cursor, ce)
    if cursor < span_end:
        ranges.append({"source": source, "start": cursor, "end": span_end})
    return ranges


def main() -> int:
    ap = argparse.ArgumentParser(description="Filter keep ranges from edl_trimmed.json.")
    ap.add_argument("edl_trimmed", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument(
        "--keep-silences",
        action="store_true",
        help=(
            "Keep ALL silences: drop only the spoken-word cut segments, preserving "
            "the deliberate pauses (TTS slots). Used by the ranking pipeline; the "
            "post-TTS de-silencer trims whatever pauses end up unused."
        ),
    )
    args = ap.parse_args()

    try:
        edl = json.loads(args.edl_trimmed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {args.edl_trimmed}: {exc}", file=sys.stderr)
        return 2

    source = edl.get("source", "")
    segments = edl.get("segments", [])
    if not isinstance(segments, list):
        print(f"error: {args.edl_trimmed} has no 'segments' list", file=sys.stderr)
        return 2

    if args.keep_silences:
        ranges = _word_cut_ranges(segments, source)
    else:
        ranges = []
        for idx, s in enumerate(segments):
            if s.get("status") != "keep":
                continue
            try:
                start = float(s["start"])
                end = float(s["end"])
            except (KeyError, TypeError, ValueError) as exc:
                print(
                    f"error: segment[{idx}] missing/invalid start|end: {exc}",
                    file=sys.stderr,
                )
                return 2
            if end <= start:
                print(
                    f"warning: skipping segment[{idx}] with non-positive duration "
                    f"(start={start}, end={end})",
                    file=sys.stderr,
                )
                continue
            ranges.append({"source": source, "start": start, "end": end})

    if not ranges:
        # derive_transcript.py and render_edl.py both refuse empty ranges, so
        # surface this here with a clearer message rather than letting them fail.
        print(
            f"error: no segments with status=='keep' in {args.edl_trimmed}",
            file=sys.stderr,
        )
        return 2

    out = {"source": source, "ranges": ranges}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    total = sum(r["end"] - r["start"] for r in ranges)
    print(f"keep ranges: {len(ranges)} ({total:.2f}s)")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
