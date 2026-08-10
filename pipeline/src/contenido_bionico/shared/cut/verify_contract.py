"""Report-only validator for the cut contract.

Reads the artifacts inside a run dir produced by the cut orchestrator
and writes a single JSON report at `<run_dir>/logs/Prep_Contract_Report.json`
listing any warnings. By default it returns 1 on any warning (suitable for
stricter callers). With `--report-only` it never returns non-zero — the
report is informational.

Checks:
  - source.mp4 exists, is non-empty, ffprobe-readable.
  - transcript.json exists and has monotonic words.
  - normalized words(transcript) form an ORDERED subsequence of the
    normalized words(final.txt), using the mapper's OWN normalization and
    matching functions so this pass can never disagree with the alignment
    the mapper accepted. Order matters: the previous set-membership check
    passed even when render ranges duplicated or reordered approved text.
    Skipping final units is allowed (a surgical cut on the resurface path
    legitimately drops approved words); matching out of order is not.
  - last word.end <= ffprobe(source.mp4).duration + small tolerance.
  - rendered duration of source.mp4 ~= sum of edl_render.json keep ranges
    (within DURATION_TOLERANCE_SECONDS).

Usage:
    python verify_contract.py --run-dir <run_dir>
    python verify_contract.py --run-dir <run_dir> --report-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure `from contenido_bionico...` resolves when this file is invoked as a script.
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from contenido_bionico.shared.ffmpeg import probe_duration_or_none  # noqa: E402

# Reuse the mapper's own normalization instead of a local re-implementation.
# A previous copy here had drifted (no elongation collapse, the old stricter
# false-start rule, no per-part edge-punctuation re-strip, no truncated-stem
# recovery, no concatenated-source-word splits), so this validator could
# reject alignments the mapper had legitimately produced.
from contenido_bionico.shared.cut.mapper import (  # noqa: E402
    _try_split_concatenated,
    final_alignment_units,
    norm,
    source_match_sequences,
    tokenize_final,
)
# render_edl appends this deterministic freeze+silence tail to source.mp4 (a
# breathing beat after the last word), so the rendered file is expected to be
# exactly TAIL_PAD_S longer than the sum of the kept EDL ranges.
from contenido_bionico.shared.cut.render_edl import TAIL_PAD_S  # noqa: E402


# Rendered-duration tolerance for shorts: the sum of edl_render keep ranges
# must match ffprobe(source.mp4) within this many seconds. Cuts are
# word-boundary EDL ranges rendered by a sample-accurate concat, so a drift
# past a quarter second means the EDL and the rendered file disagree.
DURATION_TOLERANCE_SECONDS = 0.25


def ffprobe_duration(path: Path) -> float | None:
    return probe_duration_or_none(path)


def _find_units_at_or_after(
    final_units: list[str], seq: tuple[str, ...] | list[str], start: int
) -> int | None:
    """Smallest index >= `start` where `seq` occurs contiguously in
    `final_units`. Returns None when it does not occur there."""
    seq = tuple(seq)
    if not seq:
        return None
    limit = len(final_units) - len(seq)
    for idx in range(start, limit + 1):
        if tuple(final_units[idx : idx + len(seq)]) == seq:
            return idx
    return None


def check(run_dir: Path) -> dict:
    warnings: list[str] = []
    # `notes` are informational (benign tokenization edge cases) and, unlike
    # `warnings`, do NOT fail the contract (ok = not warnings).
    notes: list[str] = []

    source = run_dir / "source.mp4"
    transcript = run_dir / "transcript.json"
    intermediates = run_dir / "_intermediates"
    final_txt = intermediates / "final.txt"
    edl_render = intermediates / "edl_render.json"

    if not source.exists() or source.stat().st_size <= 0:
        warnings.append(f"missing or empty source.mp4: {source}")
    if not transcript.exists() or transcript.stat().st_size <= 0:
        warnings.append(f"missing or empty transcript.json: {transcript}")

    source_duration = ffprobe_duration(source) if source.exists() else None
    if source.exists() and source_duration is None:
        warnings.append("ffprobe could not read source.mp4")

    transcript_data: dict = {}
    transcript_words: list[dict] = []
    if transcript.exists():
        try:
            transcript_data = json.loads(transcript.read_text(encoding="utf-8-sig"))
            transcript_words = transcript_data.get("words") or []
        except json.JSONDecodeError as e:
            warnings.append(f"transcript.json is invalid JSON: {e}")

    # Monotonic words check.
    last = -1.0
    for i, w in enumerate(transcript_words):
        try:
            s = float(w["start"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"transcript word[{i}] missing/invalid start")
            continue
        if s < last - 1e-3:
            warnings.append(
                f"transcript word[{i}] not monotonic: start={s:.3f} after {last:.3f}"
            )
        last = s

    # Last word fits inside source duration.
    if transcript_words and source_duration is not None:
        try:
            last_end = float(transcript_words[-1]["end"])
            if last_end > source_duration + 0.25:
                warnings.append(
                    f"last transcript word ends at {last_end:.3f}s but "
                    f"source.mp4 is only {source_duration:.3f}s"
                )
        except (KeyError, TypeError, ValueError):
            pass

    # Normalized transcript words must form an ORDERED subsequence of the
    # normalized final.txt words. Uses the same tokenization + match-sequence
    # machinery as the mapper: `tokenize_final` splits compound final tokens,
    # `source_match_sequences` carries the false-start collapse and
    # truncated-stem recovery, and `_try_split_concatenated` mirrors the
    # mapper's recovery for Scribe tokens that glue two/three spoken words
    # together. Order matters: the previous set-membership check passed even
    # when the render ranges duplicated or reordered approved text (both
    # post-deletion artifacts carried the same word SET). The greedy
    # earliest-end walk below is optimal for in-order matching (matching a
    # word as early as possible never blocks a later word) and naturally
    # tolerates skipped final units — a surgical cut on the resurface path
    # legitimately drops approved words — while rejecting out-of-order text.
    if final_txt.exists() and transcript_words:
        try:
            approved_text = final_txt.read_text(encoding="utf-8-sig")
            final_units = final_alignment_units(tokenize_final(approved_text))
            approved_tokens = set(final_units)
            transcript_units: set[str] = set()
            missing: list[str] = []
            pos = 0
            for w in transcript_words:
                raw = str(w.get("text", w.get("word", "")))
                sequences = source_match_sequences(raw)
                for seq in sequences:
                    transcript_units.update(seq)
                if not sequences:
                    continue
                best_end: int | None = None
                for seq in sequences:
                    idx = _find_units_at_or_after(final_units, seq, pos)
                    if idx is not None:
                        end = idx + len(seq)
                        if best_end is None or end < best_end:
                            best_end = end
                if best_end is not None:
                    pos = best_end
                    continue
                split_parts = _try_split_concatenated(norm(raw), approved_tokens)
                if split_parts is not None:
                    idx = _find_units_at_or_after(final_units, split_parts, pos)
                    if idx is not None:
                        transcript_units.update(split_parts)
                        pos = idx + len(split_parts)
                        continue
                # Unmatched: record it, do NOT advance pos — the walk resyncs
                # on the next word that still matches in order.
                missing.append(norm(raw))
            if missing:
                # Only flag a few — long lists are noise.
                preview = ", ".join(missing[:8])
                more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
                warnings.append(
                    f"transcript has {len(missing)} word(s) that cannot be "
                    f"matched IN ORDER against final.txt approved text: "
                    f"[{preview}]{more}"
                )
            # Inverse check: approved words missing from transcript — if too
            # many show up here, cut dropped them.
            dropped_from_approved = [
                t for t in approved_tokens if t and t not in transcript_units
            ]
            if dropped_from_approved:
                preview = ", ".join(sorted(dropped_from_approved)[:8])
                more = (
                    f" (+{len(dropped_from_approved) - 8} more)"
                    if len(dropped_from_approved) > 8
                    else ""
                )
                msg = (
                    f"final.txt has {len(dropped_from_approved)} approved "
                    f"word(s) missing from transcript.json: [{preview}]{more}"
                )
                # A few dropped approved words are benign Scribe tokenization
                # edge cases (glue / casing / elongation); discarding an
                # editor+reviewer-approved cut over them is wrong. Only a larger
                # divergence means the cut actually lost approved content, so
                # fail only past a small, length-scaled tolerance.
                tolerance = max(2, round(0.02 * len(approved_tokens)))
                if len(dropped_from_approved) <= tolerance:
                    notes.append(f"{msg} (within tolerance {tolerance}; non-fatal)")
                else:
                    warnings.append(msg)
        except OSError as e:
            warnings.append(f"could not read final.txt: {e}")

    # Source duration matches edl_render keep ranges within tolerance.
    if edl_render.exists() and source_duration is not None:
        try:
            edl_data = json.loads(edl_render.read_text(encoding="utf-8-sig"))
            ranges = edl_data.get("ranges") or []
            keep_total = sum(
                float(r["end"]) - float(r["start"]) for r in ranges
            )
            # source.mp4 = kept ranges + the deterministic end tail.
            expected = keep_total + TAIL_PAD_S
            diff = abs(expected - source_duration)
            if diff > DURATION_TOLERANCE_SECONDS:
                warnings.append(
                    f"sum of edl_render keep ranges ({keep_total:.3f}s) + tail "
                    f"({TAIL_PAD_S:.3f}s) differs from source.mp4 duration "
                    f"({source_duration:.3f}s) by {diff:.3f}s "
                    f"(tolerance {DURATION_TOLERANCE_SECONDS}s)"
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            warnings.append(f"could not validate edl_render.json: {e}")

    return {
        "run_dir": str(run_dir),
        "source_duration_seconds": source_duration,
        "transcript_word_count": len(transcript_words),
        "warnings": warnings,
        "notes": notes,
        "ok": not warnings,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0; just write the report. Default is to exit 0 only "
        "if there are no warnings.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Path to write the report JSON. Default: <run_dir>/logs/Prep_Contract_Report.json",
    )
    args = p.parse_args()

    if not args.run_dir.exists():
        print(f"ERROR: run dir not found: {args.run_dir}", file=sys.stderr)
        return 2

    report = check(args.run_dir)

    out_path = args.out or (args.run_dir / "logs" / "Prep_Contract_Report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if report["warnings"]:
        print(f"verify_contract: {len(report['warnings'])} warning(s)")
        for w in report["warnings"]:
            print(f"  WARN: {w}")
    else:
        print("verify_contract: OK")
    for n in report.get("notes") or []:
        print(f"  NOTE: {n}")

    if args.report_only:
        return 0
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
