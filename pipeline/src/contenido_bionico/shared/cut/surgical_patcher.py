"""Deterministic surgical patcher for mapper-rejected `final.txt`.

Runs between mapper attempts when the mapper rejects the editor-reviewer
loop's output. Reads the mapper error log, parses its `Issue inventory`
section (each entry is a contiguous range of final tokens that cannot be
matched against the source), locates each offending phrase in
`final.txt`, and deletes it. Light grammatical cleanup follows so the
resulting text reads as a deletion-only subset of the original.

Why deterministic instead of an LLM:
  - The mapper already pinpoints which tokens fail with full context.
    There's nothing to "reason" about; the action is literally "remove
    these specific tokens".
  - The Finalizer agent has demonstrated repeatedly that it over-deletes
    when given an ambiguous prompt (we've seen a single bad iteration
    cut 90% of the transcript). A deterministic patcher has 0 risk of
    that failure mode.
  - 0 Claude cost, 0 wait time.

Invocation contract (matches what `orchestrator.py` shells out):
    python surgical_patcher.py --video-id <N> --mapper-error <path>

Reads:
  - `runs/<id>/_intermediates/final.txt`            (current text)
  - mapper error log (passed via --mapper-error)

Writes:
  - `runs/<id>/_intermediates/final.txt`            (overwritten)
  - `runs/<id>/_intermediates/patcher/patch_<N>.txt`         (before / after audit copies)
  - `runs/<id>/_intermediates/patcher/patch_<N>.diff`        (unified diff)
  - `runs/<id>/_intermediates/patcher/patch_<N>.report.txt`  (per-issue outcome)
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
RUNS = REPO_ROOT / "runs"

# Shrink guard: reject any patch that would delete more than this fraction of
# final.txt (whitespace-token count). A repair that big is not "surgical" —
# it means the editor's text diverges massively from the source and needs a
# fresh editor-loop pass, not a mechanical deletion. Exit code 32 tells the
# cut orchestrator to escalate to the editor-loop recovery path.
SHRINK_GUARD_RATIO = 0.20
SHRINK_GUARD_EXIT_CODE = 32


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _norm_word(token: str) -> str:
    """Normalize a single editor token the same way the mapper does:
    lowercase, strip accents, strip leading/trailing punctuation."""
    if not token:
        return ""
    t = _strip_accents(token.lower())
    t = re.sub(r"^[^\wáéíóúñü]+|[^\wáéíóúñü]+$", "", t)
    return t


@dataclass
class Issue:
    """One entry parsed from the mapper error log's Issue inventory."""
    num: int
    final_idx_start: int
    final_idx_end: int
    length: int
    raw_preview: str            # e.g. "Atlassian," — exact surface form
    normalized: str             # e.g. "atlassian"
    context_before: str         # whitespace-joined normalized tokens
    context_after: str          # whitespace-joined normalized tokens


_ISSUE_HEADER_RE = re.compile(
    r"^Issue (\d+): final tokens \[(\d+)\.\.(\d+)\], (\d+) token\(s\)$"
)


def parse_issue_inventory(mapper_log_path: Path) -> list[Issue]:
    """Walk the mapper error log and return one Issue per inventory entry.

    The log format is the one produced by
    `mapper._write_error_log()` — a block per issue with `Issue N:` header
    followed by four fixed `key: value` lines (raw preview, normalized,
    context before, context after). Anything that doesn't parse cleanly is
    skipped — we'd rather under-patch than crash on a log-format drift.
    """
    if not mapper_log_path.is_file():
        return []
    text = mapper_log_path.read_text(encoding="utf-8", errors="replace")
    issues: list[Issue] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _ISSUE_HEADER_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        num = int(m.group(1))
        start = int(m.group(2))
        end = int(m.group(3))
        length = int(m.group(4))
        fields = {"raw preview": "", "normalized": "", "context before": "", "context after": ""}
        j = i + 1
        while j < len(lines) and j - i <= 6:
            stripped = lines[j].strip()
            for key in fields:
                marker = f"{key}:"
                if stripped.startswith(marker):
                    fields[key] = stripped[len(marker):].strip()
                    break
            if stripped.startswith("Issue ") or stripped.startswith("=== "):
                break
            j += 1
        issues.append(
            Issue(
                num=num,
                final_idx_start=start,
                final_idx_end=end,
                length=length,
                raw_preview=fields["raw preview"],
                normalized=fields["normalized"],
                context_before=fields["context before"].lstrip(".").strip(),
                context_after=fields["context after"].rstrip(".").strip(),
            )
        )
        i = j
    return issues


def _split_whitespace_tokens(text: str) -> list[tuple[int, int, str]]:
    """Return `(start, end, token)` for every whitespace-separated token.

    Used to walk `final.txt` token-by-token while remembering each token's
    byte offsets so we can delete precisely.
    """
    out: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\S+", text):
        out.append((m.start(), m.end(), m.group()))
    return out


def _norm_token_sequence(tokens: list[str]) -> list[str]:
    """List of normalized forms, skipping empties so the contexts compare
    apples-to-apples with what the mapper recorded in the log."""
    out: list[str] = []
    for t in tokens:
        n = _norm_word(t)
        if n:
            out.append(n)
    return out


def _find_issue_span(
    text: str, token_span: list[tuple[int, int, str]], issue: Issue
) -> tuple[int, int] | None:
    """Locate the byte range in `text` that corresponds to `issue.raw_preview`.

    Strategy:
      1. Tokenize `issue.context_before`, `issue.raw_preview`, and
         `issue.context_after` into normalized forms.
      2. Slide over `token_span` looking for a window where the normalized
         tokens match `context_before + raw_preview + context_after`.
      3. Return the byte range of the raw-preview tokens in the match.

    Returns None if no unique match is found. The patcher then logs the
    miss and skips the issue rather than guess.
    """
    before_norms = _norm_token_sequence(issue.context_before.split())
    after_norms = _norm_token_sequence(issue.context_after.split())
    preview_tokens = issue.raw_preview.split()
    preview_norms = _norm_token_sequence(preview_tokens)
    if not preview_norms:
        return None
    norms = [_norm_word(t) for _, _, t in token_span]
    # Drop empty norms (pure-punct tokens) from the search while
    # remembering each surviving token's original index.
    indexed: list[tuple[int, str]] = [
        (k, n) for k, n in enumerate(norms) if n
    ]
    norm_only = [n for _, n in indexed]
    needle = before_norms + preview_norms + after_norms
    matches: list[int] = []
    for start in range(0, len(norm_only) - len(needle) + 1):
        if norm_only[start : start + len(needle)] == needle:
            matches.append(start)
    if len(matches) != 1:
        # Try a more permissive fallback: just look for preview_norms
        # immediately preceded by the last 2 norms of context_before.
        if not before_norms:
            return None
        short_before = before_norms[-2:]
        short_needle = short_before + preview_norms
        matches = []
        for start in range(0, len(norm_only) - len(short_needle) + 1):
            if norm_only[start : start + len(short_needle)] == short_needle:
                matches.append(start)
        if len(matches) != 1:
            return None
        match_start_in_indexed = matches[0] + len(short_before)
    else:
        match_start_in_indexed = matches[0] + len(before_norms)
    preview_len = len(preview_norms)
    # Translate the indexed positions back to original token-span indices.
    first_token_idx = indexed[match_start_in_indexed][0]
    last_token_idx = indexed[match_start_in_indexed + preview_len - 1][0]
    return token_span[first_token_idx][0], token_span[last_token_idx][1]


def _cleanup_artifacts(text: str) -> str:
    """Tidy obvious grammatical residue from the deletions.

    Conservative: only the patterns we know are safe.
      - Collapse runs of duplicated function words ("a a", "el el", "que que")
        introduced when the patcher removed the word between them.
      - Collapse double commas, double spaces, comma-before-comma.
      - Strip orphan punctuation at sentence start ("?, Hola" → "Hola").
    """
    # Collapse identical adjacent function words (case-insensitive). Limit
    # to short words (≤4 chars) so we don't accidentally drop a real
    # rhetorical repeat like "muy muy bien".
    DUPS = ("a", "y", "o", "e", "el", "la", "un", "una", "que", "de", "en", "no", "se", "le")
    pattern = (
        r"\b(" + "|".join(re.escape(w) for w in DUPS) + r")\s+\1\b"
    )
    text = re.sub(pattern, r"\1", text, flags=re.IGNORECASE)
    # Comma noise: ", ," → ",", ".." → "." (but keep "..." ellipsis).
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\.(?!\.)\s*\.(?!\.)", ".", text)
    # Multiple spaces collapse to one.
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Leading punctuation on sentence start (after ". " or at file start).
    text = re.sub(r"(^|\. )([,;:])\s*", r"\1", text)
    return text


def patch(text: str, issues: list[Issue]) -> tuple[str, list[dict]]:
    """Apply every locatable issue as a deletion. Return (new_text, report).

    Each issue produces one report row with the outcome
    (`applied` / `not_found`) and the bytes removed if applied. Issues are
    processed right-to-left so earlier byte offsets stay valid as we
    splice the string.
    """
    token_span = _split_whitespace_tokens(text)
    rows: list[dict] = []
    spans_to_remove: list[tuple[int, int, Issue]] = []
    for issue in issues:
        span = _find_issue_span(text, token_span, issue)
        if span is None:
            rows.append({
                "issue": issue.num,
                "raw_preview": issue.raw_preview,
                "outcome": "not_found",
                "removed_bytes": 0,
            })
            continue
        spans_to_remove.append((span[0], span[1], issue))
    # Sort by end position descending; splice from the back forward.
    spans_to_remove.sort(key=lambda x: x[0], reverse=True)
    new_text = text
    for start, end, issue in spans_to_remove:
        removed = new_text[start:end]
        # Also swallow one trailing whitespace/comma/period to keep the
        # surrounding punctuation tight. The cleanup pass picks up the
        # rest.
        eat_end = end
        while eat_end < len(new_text) and new_text[eat_end] in " ,;:":
            eat_end += 1
            if eat_end - end >= 2:
                break
        new_text = new_text[:start] + new_text[eat_end:]
        rows.append({
            "issue": issue.num,
            "raw_preview": issue.raw_preview,
            "outcome": "applied",
            "removed_bytes": len(removed),
        })
    new_text = _cleanup_artifacts(new_text)
    rows.sort(key=lambda r: r["issue"])
    return new_text, rows


def _next_iteration(run_dir: Path) -> int:
    out_dir = run_dir / "_intermediates" / "patcher"
    if not out_dir.is_dir():
        return 1
    highest = 0
    for child in out_dir.iterdir():
        m = re.match(r"^patch_(\d+)\.txt$", child.name)
        if not m:
            continue
        n = int(m.group(1))
        if n > highest:
            highest = n
    return highest + 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic surgical patcher for mapper-rejected final.txt."
    )
    ap.add_argument("--video-id", required=True)
    ap.add_argument(
        "--mapper-error",
        type=Path,
        required=True,
        help="Path to the mapper_<stamp>_error.log file produced by mapper.py.",
    )
    args = ap.parse_args(argv)

    run_dir = RUNS / str(args.video_id)
    final_path = run_dir / "_intermediates" / "final.txt"
    if not final_path.is_file() or final_path.stat().st_size == 0:
        print(
            f"ERROR: final.txt missing or empty at {final_path}", file=sys.stderr
        )
        return 30
    if not args.mapper_error.is_file():
        print(
            f"ERROR: mapper error log missing at {args.mapper_error}",
            file=sys.stderr,
        )
        return 30

    issues = parse_issue_inventory(args.mapper_error)
    if not issues:
        print(
            "ERROR: no parsable Issue inventory in mapper log — nothing to patch.",
            file=sys.stderr,
        )
        return 31

    before = final_path.read_text(encoding="utf-8")
    after, rows = patch(before, issues)

    # Shrink guard: never delete a large chunk of the approved transcript.
    # final.txt is left untouched; the dedicated exit code lets the
    # orchestrator escalate to the editor-loop recovery path.
    before_words = len(before.split())
    after_words = len(after.split())
    if before_words > 0 and after_words < before_words * (1.0 - SHRINK_GUARD_RATIO):
        print(
            (
                f"ERROR: surgical patch would shrink final.txt from "
                f"{before_words} to {after_words} words (more than "
                f"{SHRINK_GUARD_RATIO:.0%}); rejecting the patch — "
                f"final.txt left untouched."
            ),
            file=sys.stderr,
        )
        return SHRINK_GUARD_EXIT_CODE

    iteration = _next_iteration(run_dir)
    patcher_dir = run_dir / "_intermediates" / "patcher"
    patcher_dir.mkdir(parents=True, exist_ok=True)
    out_text = patcher_dir / f"patch_{iteration}.txt"
    out_diff = patcher_dir / f"patch_{iteration}.diff"
    out_report = patcher_dir / f"patch_{iteration}.report.txt"

    out_text.write_text(after, encoding="utf-8")
    final_path.write_text(after, encoding="utf-8")
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"final.txt (before patch_{iteration})",
            tofile=f"final.txt (after patch_{iteration})",
            n=3,
            lineterm="",
        )
    )
    out_diff.write_text("\n".join(diff_lines) + "\n", encoding="utf-8")

    applied = sum(1 for r in rows if r["outcome"] == "applied")
    skipped = sum(1 for r in rows if r["outcome"] == "not_found")
    total_removed = sum(r["removed_bytes"] for r in rows)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_lines = [
        f"=== Surgical patcher report ===",
        f"Timestamp:        {stamp}",
        f"Mapper log:       {args.mapper_error}",
        f"Issues parsed:    {len(issues)}",
        f"Applied:          {applied}",
        f"Not found:        {skipped}",
        f"Bytes removed:    {total_removed}",
        f"Before size:      {len(before)}",
        f"After  size:      {len(after)}",
        f"",
        f"=== Per-issue outcome ===",
    ]
    for r in rows:
        report_lines.append(
            f"Issue {r['issue']:>2}: {r['outcome']:<10}  {r['removed_bytes']:>4} bytes  preview={r['raw_preview']!r}"
        )
    report_lines.append("")
    out_report.write_text("\n".join(report_lines), encoding="utf-8")

    print(
        f"OK Run {args.video_id} | Patcher iter {iteration} done | "
        f"Issues: {applied}/{len(issues)} applied, {skipped} not found | "
        f"Removed {total_removed} bytes | Wrote: {final_path.name}",
        flush=True,
    )
    print(f"Patcher output:  {out_text}")
    print(f"Patcher diff:    {out_diff}")
    print(f"Patcher report:  {out_report}")
    if skipped:
        # Not a hard failure — orchestrator will re-run mapper and either
        # accept the partial patch or escalate to the Finalizer.
        print(
            f"WARN: {skipped} issue(s) could not be located in final.txt; "
            f"they remain unpatched.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
