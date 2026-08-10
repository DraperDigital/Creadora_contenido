"""Deterministic mapper — DP alignment between final.txt and word-for-word.

PHASE 1 (cut) step. The orchestrator invokes it as:
    python mapper.py _intermediates/final.txt _intermediates/word_for_word.json \
        -o _intermediates/edl.json

The mapping step is subsequence alignment, not editorial judgment:
the editor-approved final.txt is a subsequence of the verbose transcript,
and the verbose is the concatenation of tokens from Scribe's word-for-word.
The task is to decide WHICH source tokens best explain the final while
keeping continuity.

Algorithm:
    DP with two tables:
      dp_match[i][j] = best score reaching (i,j) after matching S[i-1]→T[j-1]
      dp_skip[i][j]  = best score reaching (i,j) after skipping S[i-1]

    Transitions from (i,j):
      - skip source S[i]:
          dp_skip[i+1][j] = max(dp_match[i][j], dp_skip[i][j]) + SKIP_SOURCE
      - match S[i] → T[j], if norm(S[i]) == norm(T[j]):
          new = match_score + (CONTINUITY if came from match else START_NEW_KEEP)
          common-word penalty if the word is very common
          dp_match[i+1][j+1] = max(dp_match[i][j], dp_skip[i][j]) + new

    Backpointers reconstruct which source words are keep / cut.

Post-backtrack validation:
    - Every final token has a match (no gaps).
    - Mapped text == final after normalization.
    - Soft warnings: keep segments < 0.8s, < 3 words, gaps < 80ms.

Output: edl.json — the contract consumed by trim_spacings.py:
    {
      "source": <stem>,
      "total_words_verbose": int, "total_words_edited": int,
      "n_segments": int, "n_keep_segments": int, "n_cut_segments": int,
      "total_keep_seconds": float, "total_cut_seconds": float,
      "segments": [{ "start": float, "end": float,
                     "status": "keep"|"cut", "text": str }, ...]
    }
The `segments` cover the full word-for-word range contiguously with no gaps.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np


class AlignmentFailure(RuntimeError):
    """Raised when DP alignment cannot consume every final token in order.

    Carries a `diagnostics` dict with the alignment frontier and surrounding
    context so the failure log can pinpoint where the editor diverged from
    the source instead of only saying "no path exists".
    """

    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = diagnostics


class ValidatorFailure(RuntimeError):
    """Raised AFTER the DP succeeded but the post-validation step rejected
    the alignment (kept source word does not explain the final text, tail
    duplicate detected, etc).

    Distinct from `AlignmentFailure` because the DP itself completed — the
    issue is in the sanity checks that follow. Carries the errors and
    warnings lists so `_write_error_log` can surface them to the Finalizer
    via a fresh log file instead of letting the recovery loop reuse a
    stale AlignmentFailure log.
    """

    def __init__(self, message: str, errors: list[str], warnings: list[str]):
        super().__init__(message)
        self.errors = errors
        self.warnings = warnings


def _compute_alignment_frontier(
    dp: "np.ndarray",
    N: int,
    M: int,
    NEG: int,
    source_words: list[dict],
    final_tokens: list[str],
    fin_norm: list[str],
) -> dict:
    """Find the furthest (i, j) the DP could reach before getting stuck.

    The frontier is the cell with the largest j (final tokens consumed) that
    is still reachable. When there are ties we pick the smallest i (the
    earliest source position that already consumed that many final tokens —
    typically where the editor's text first diverges from the source order).

    Returns a dict with: furthest_final_idx, source_idx_at_frontier,
    next_final_token, next_final_token_repr, source_context (a window of
    source words around the frontier with their original text + indices).
    """
    furthest_j = -1
    best_i = -1
    for i in range(N + 1):
        row_max = max(int(dp[i].max()), NEG)
        if row_max <= NEG:
            continue
        # Largest j in this row that is reachable.
        reachable_mask = (dp[i].max(axis=1) > NEG)
        if not reachable_mask.any():
            continue
        j = int(np.where(reachable_mask)[0].max())
        if j > furthest_j or (j == furthest_j and (best_i == -1 or i < best_i)):
            furthest_j = j
            best_i = i

    next_token = fin_norm[furthest_j] if 0 <= furthest_j < M else None
    next_token_raw = (
        final_tokens[furthest_j] if 0 <= furthest_j < len(final_tokens) else None
    )
    if next_token_raw is not None:
        next_token_repr = f'"{next_token_raw}" (normalized: "{next_token}")'
    else:
        next_token_repr = "<end of final tokens>"

    window = 8
    src_lo = max(0, best_i - window)
    src_hi = min(N, best_i + window)
    source_context = [
        {
            "index": idx,
            "text": source_words[idx]["text"],
            "start": float(source_words[idx].get("start", 0.0)),
            "end": float(source_words[idx].get("end", 0.0)),
            "marker": "<-- frontier" if idx == best_i else "",
        }
        for idx in range(src_lo, src_hi)
    ]

    final_lo = max(0, furthest_j - window)
    final_hi = min(M, furthest_j + window)
    final_context = [
        {
            "index": idx,
            "normalized": fin_norm[idx],
            "raw": final_tokens[idx] if idx < len(final_tokens) else None,
            "marker": "<-- could not consume" if idx == furthest_j else "",
        }
        for idx in range(final_lo, final_hi)
    ]

    return {
        "furthest_final_idx": furthest_j,
        "source_idx_at_frontier": best_i,
        "next_final_token": next_token,
        "next_final_token_repr": next_token_repr,
        "source_context": source_context,
        "final_context": final_context,
    }


def _build_skipped_range(
    start: int, end: int, final_tokens: list[str], fin_norm: list[str]
) -> dict:
    """Describe one contiguous range of skipped final tokens.

    `start` and `end` are inclusive indices into `fin_norm`. Returns a
    dict with the index bounds plus the raw text snippet so the
    Finalizer can locate the offending phrase in `<CURRENT_FINAL>`
    without counting tokens.
    """
    M = len(fin_norm)
    span_norms = fin_norm[start : end + 1]
    # Pull a short raw-token preview from `final_tokens`. fin_norm can
    # have more entries than final_tokens (each compound token may emit
    # multiple alignment units) so we walk in order and capture the raw
    # surface form of the affected region.
    raw_preview = " ".join(_raw_tokens_for_norm_range(final_tokens, fin_norm, start, end))
    context_before = " ".join(fin_norm[max(0, start - 4) : start])
    context_after = " ".join(fin_norm[end + 1 : min(M, end + 5)])
    return {
        "final_idx_start": int(start),
        "final_idx_end": int(end),
        "length": int(end - start + 1),
        "normalized_tokens": span_norms,
        "raw_preview": raw_preview,
        "context_before": context_before,
        "context_after": context_after,
    }


def _raw_tokens_for_norm_range(
    final_tokens: list[str], fin_norm: list[str], start: int, end: int
) -> list[str]:
    """Best-effort raw-token preview for a [start, end] range of fin_norm.

    `tokenize_final` may split one whitespace-token into several units;
    we walk raw tokens in order and accumulate any whose alignment units
    overlap the requested range. Returns the raw surface forms verbatim,
    which is what the Finalizer needs to locate the phrase visually.
    """
    out: list[str] = []
    cursor = 0
    for raw in final_tokens:
        units = alignment_units_for_token(raw)
        unit_count = len(units)
        if unit_count == 0:
            continue
        unit_start = cursor
        unit_end = cursor + unit_count - 1
        if unit_end >= start and unit_start <= end:
            out.append(raw)
        cursor = unit_end + 1
        if cursor > end:
            break
    return out


def _frontier_from_first_skip(
    first_range: dict, source_words: list[dict], final_tokens: list[str], fin_norm: list[str]
) -> dict:
    """Construct a frontier-style dict from the first skipped range.

    Older log readers and the Finalizer's previous prompt format both
    expected a single `frontier` block. After the DP refactor every
    issue lives in `skipped_ranges`; this helper exposes the first
    range under the legacy schema so the rest of the diagnostics stay
    backward compatible.
    """
    start = first_range["final_idx_start"]
    next_norm = fin_norm[start] if start < len(fin_norm) else ""
    next_raw = (
        _raw_tokens_for_norm_range(final_tokens, fin_norm, start, start)
        or [next_norm]
    )[0]
    M = len(fin_norm)
    final_context = []
    for fj in range(max(0, start - 4), min(M, start + 5)):
        final_context.append(
            {
                "index": fj,
                "raw": _raw_tokens_for_norm_range(final_tokens, fin_norm, fj, fj)[0]
                if _raw_tokens_for_norm_range(final_tokens, fin_norm, fj, fj)
                else "",
                "normalized": fin_norm[fj],
                "marker": "<-- first skipped" if fj == start else "",
            }
        )
    return {
        "furthest_final_idx": start,
        "source_idx_at_frontier": None,
        "next_final_token_repr": f'"{next_raw}" (normalized: "{next_norm}")',
        "final_context": final_context,
        "source_context": [],
    }


def _write_error_log(
    output_path: Path,
    final_txt_path: Path,
    word_for_word_path: Path,
    error: BaseException,
    extra: dict | None = None,
) -> Path:
    """Write a structured error log when the mapper fails.

    Returns the log path so the caller can print it.
    """
    log_dir = output_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_dir / f"mapper_{ts}_error.log"

    sections: list[str] = []
    sections.append("=== contenido-bionico mapper error log ===")
    sections.append(f"Timestamp:        {ts}")
    sections.append(f"Final input:      {final_txt_path}")
    sections.append(f"Source input:     {word_for_word_path}")
    sections.append(f"Intended output:  {output_path}")
    sections.append(f"Error type:       {type(error).__name__}")
    sections.append(f"Error message:    {error}")
    sections.append("")

    if extra:
        for key, value in extra.items():
            sections.append(f"{key}: {value}")
        sections.append("")

    if isinstance(error, AlignmentFailure):
        diag = error.diagnostics
        sections.append("=== Diagnostics ===")
        sections.append(f"Mode:                  {diag.get('mode')}")
        issues = diag.get("issues") or []
        if issues:
            sections.append(f"Issues detected:       {', '.join(issues)}")
        sections.append(f"Total source words:    {diag.get('total_source_words')}")
        sections.append(f"Total final tokens:    {diag.get('total_final_tokens')}")
        missing_count = diag.get("missing_count", 0)
        sections.append(f"Missing word count:    {missing_count}")
        if missing_count:
            full_missing = diag.get("missing_all") or diag.get("missing_sample", [])
            sample = diag.get("missing_sample", [])
            if sample:
                sections.append("Missing word sample (up to 32):")
                for w in sample:
                    sections.append(f"  - {w}")
            if full_missing and len(full_missing) > len(sample):
                sections.append(
                    f"... ({len(full_missing) - len(sample)} more not shown above)"
                )
        skipped_count = diag.get("skipped_final_count")
        skipped_ranges = diag.get("skipped_ranges") or []
        if skipped_count is not None:
            sections.append(
                f"Skipped final tokens:  {skipped_count} across "
                f"{len(skipped_ranges)} contiguous range(s)"
            )
        sections.append("")

        # Per-range issue inventory. Each range is a contiguous block of
        # final tokens that the Finalizer must delete (or include in a
        # whole-sentence deletion). The raw preview tells the agent what
        # to look for in `<CURRENT_FINAL>`; the surrounding context (a
        # few normalized tokens on each side) helps disambiguate when
        # the same phrase appears more than once.
        if skipped_ranges:
            sections.append("=== Issue inventory (every range must be deleted) ===")
            for idx, r in enumerate(skipped_ranges, 1):
                sections.append(
                    f"Issue {idx}: final tokens "
                    f"[{r['final_idx_start']}..{r['final_idx_end']}], "
                    f"{r['length']} token(s)"
                )
                sections.append(f"  raw preview:    {r.get('raw_preview', '')}")
                sections.append(
                    f"  normalized:     {' '.join(r.get('normalized_tokens', []))}"
                )
                if r.get("context_before"):
                    sections.append(f"  context before: ...{r['context_before']}")
                if r.get("context_after"):
                    sections.append(f"  context after:  {r['context_after']}...")
            sections.append("")

        # Legacy frontier block (always emitted with at least the first
        # skipped range when present, so downstream consumers / older
        # prompts that still look for it continue to work).
        frontier = diag.get("frontier") or {}
        if frontier:
            sections.append("=== Alignment frontier (first skipped range) ===")
            sections.append(
                f"Furthest final token index reached: "
                f"{frontier.get('furthest_final_idx')} of {diag.get('total_final_tokens')}"
            )
            src_idx = frontier.get("source_idx_at_frontier")
            if src_idx is not None:
                sections.append(
                    f"Source word index at frontier:      "
                    f"{src_idx} of {diag.get('total_source_words')}"
                )
            sections.append(
                f"Next final token DP could not consume: "
                f"{frontier.get('next_final_token_repr')}"
            )
            sections.append("")
            if frontier.get("source_context"):
                sections.append("=== Source words around the frontier ===")
                sections.append(
                    "(idx)  start..end   text                    marker"
                )
                for w in frontier.get("source_context", []):
                    sections.append(
                        f"  {w['index']:>5}  {w['start']:>6.2f}..{w['end']:<6.2f}  "
                        f"{w['text']:<24} {w['marker']}"
                    )
                sections.append("")
            if frontier.get("final_context"):
                sections.append("=== Final tokens around the frontier ===")
                sections.append("(idx)  raw                      normalized              marker")
                for t in frontier.get("final_context", []):
                    raw_display = t.get("raw") if t.get("raw") is not None else "<none>"
                    sections.append(
                        f"  {t['index']:>5}  {str(raw_display):<24} {t['normalized']:<22}  {t['marker']}"
                    )
                sections.append("")
    elif isinstance(error, ValidatorFailure):
        sections.append("=== Diagnostics ===")
        sections.append("Mode:                  validator_rejected")
        sections.append("Issues detected:       validator_errors")
        if extra:
            for key in ("source words", "final tokens", "keep words", "n segments"):
                if key in (extra or {}):
                    sections.append(f"{key:<22} {extra[key]}")
        sections.append("")
        sections.append("=== Validator errors (every entry must be resolved) ===")
        for idx, err_text in enumerate(error.errors, 1):
            sections.append(f"Issue {idx}: {err_text}")
        sections.append("")
        if error.warnings:
            sections.append("=== Validator warnings (informational) ===")
            for w in error.warnings[:32]:
                sections.append(f"  WARN: {w}")
            sections.append("")
    else:
        sections.append("=== Traceback ===")
        sections.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))

    log_path.write_text("\n".join(sections), encoding="utf-8")
    return log_path


SCORING = {
    "MATCH": 10,
    "CONTINUITY": 2,
    "START_NEW_KEEP": -4,
    "SKIP_SOURCE": -1,
    "SKIP_FINAL": -100,
    "ISOLATED_COMMON_WORD_PENALTY": -50,
}

COMMON_WORDS = {
    "y", "el", "la", "un", "una", "de", "en", "a", "o", "pero", "que",
    "lo", "los", "las", "le", "les", "se", "te", "me", "es", "no", "si",
    "su", "sus", "del", "al", "ya", "mi", "tu", "por", "con", "para",
}

VAL_MIN_KEEP_SECONDS = 0.8
VAL_MIN_KEEP_WORDS = 3
VAL_TINY_GAP_SECONDS = 0.08
# First keep range starting deeper than this into the source usually means
# the editor deleted the video's opening hook. Warning only (a long silent
# or discarded intro is legitimate), but printed loudly by main().
HOOK_START_WARN_SECONDS = 12.0

CONNECTOR_PHRASES = {
    "asi que",
    "ahora bien",
    "lo que",
    "y",
    "pero",
    "entonces",
    "que",
    "porque",
}
TAIL_DUP_MAX_LOOKBACK = 8


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


_ELONGATION_RE = re.compile(r"([a-záéíóúñü])\1{2,}", re.IGNORECASE)


def _collapse_elongations(text: str) -> str:
    """Collapse runs of 3+ identical letters to a single letter.

    Scribe transcribes emphatic speech as elongated tokens (`cuándooo`,
    `ehhhh`, `siii`). The editor's clean version drops the elongation
    (`cuándo`, `eh`, `si`). The mapper's `norm()` keeps the run of o's so
    the comparison fails. Collapsing 3+ repeats to 1 brings the two into
    sync without affecting legitimate doubled letters (Spanish has `ll`,
    `rr`, `cc`, `nn` as valid double consonants, all length 2; we only
    touch runs of 3+).
    """
    return _ELONGATION_RE.sub(r"\1", text)


def norm(token: str) -> str:
    """Lowercase, strip accents, strip leading/trailing punctuation,
    collapse vowel/consonant elongations of 3+ repeats."""
    if not token:
        return ""
    t = strip_accents(token.lower())
    t = re.sub(r"^[^\wáéíóúñü]+|[^\wáéíóúñü]+$", "", t)
    t = _collapse_elongations(t)
    return t


_EDGE_PUNCT_RE = re.compile(r"^[^\wáéíóúñü]+|[^\wáéíóúñü]+$")


def alignment_units_for_token(token: str) -> list[str]:
    """Return normalized units used by the mapper alignment.

    Scribe and the cleaned final text do not always agree on whitespace token
    boundaries for URLs, hyphenated compounds, and spoken false starts. The
    mapper aligns on these units while still keeping/cutting whole timed source
    words.

    Each part has its own edge punctuation re-stripped after the split.
    Without this, source tokens like `vertical.¿Vale?` produce parts
    `['vertical', '¿vale']` (the `¿` was internal to the original token, so
    `norm()` couldn't strip it on the outer pass). The editor's whitespace
    tokenization gives `['vertical', '¿Vale?']` → norm → `['vertical',
    'vale']`. The DP then mismatches `¿vale` vs `vale`. Re-stripping per
    part brings both sides to the same units.
    """
    base = norm(token)
    if not base:
        return []
    parts = [p for p in re.split(r"[.\-/]+", base) if p]
    parts = [_EDGE_PUNCT_RE.sub("", p) for p in parts]
    parts = [p for p in parts if p]
    return parts or [base]


def final_alignment_units(final_tokens: list[str]) -> list[str]:
    units: list[str] = []
    for token in final_tokens:
        units.extend(alignment_units_for_token(token))
    return units


def _dedupe_sequences(sequences: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    seen: set[tuple[str, ...]] = set()
    out: list[tuple[str, ...]] = []
    for seq in sequences:
        if not seq or seq in seen:
            continue
        seen.add(seq)
        out.append(seq)
    return out


def _edit_distance_within(a: str, b: str, limit: int) -> bool:
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
            row_min = min(row_min, cur[-1])
        if row_min > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def _is_false_start_fragment(part: str, nxt: str) -> bool:
    """Heuristic: is `part` a fragment of a stutter / false start that
    Scribe glued to the actual word `nxt` with a hyphen (or any internal
    boundary)?

    Liberal-by-design: in practice Spanish speakers produce single-letter
    false starts ALL the time (`a-`, `d-`, `e-`, `y-`) and Scribe will
    happily transcribe them as `a-ahora`, `d-dar`, etc. We used to require
    `len(part) >= 2` and `part[:2] == nxt[:2]` for safety, but that
    rejected the most common Spanish-speech case and the only "compound"
    classes those checks protected against (`co-founder`, `dm-setters`,
    `mp-3`) are very rare in this domain. The cost of being too liberal
    is occasionally collapsing a legitimate compound, leaving the resulting
    cortado with a barely-audible prefix sound — vs. the cost of being too
    strict, which was 8 of our 16 alignment failures forcing an LLM
    recovery cycle.

    Returns True if any of:
      - part == nxt (literal repeat)
      - nxt starts with part (prefix repeat — handles `meta-` / `metanos`
        where the speaker said the first 4 letters then re-said the word)
      - edit distance ≤ ceil(len(nxt)/2) (catches near-prefix typos like
        `altas-atlassian` where the speaker said "atlas" then "atlassian"
        but Scribe heard "altas" — 1-edit away from "atlas")
    """
    if not part or not nxt:
        return False
    if part == nxt:
        return True
    if len(part) > len(nxt):
        return False
    if nxt.startswith(part):
        return True
    # First-letter-match shortcut: if the false-start fragment shares its
    # opening consonant/vowel with the full word AND is at most half of
    # the full word's length, accept it. Catches the Spanish false-start
    # patterns where the speaker says only the first 1-3 letters before
    # restarting (`a-ahora`, `cos-customizas`, `altas-atlassian` — the
    # latter survives even though "altas" is 4 edits from "atlas" because
    # 5 ≤ ceil(9/2) = 5). Distinct compounds like `co-founder` or
    # `dm-setters` fail the first-letter test (c ≠ f, d ≠ s) and are
    # protected.
    if part[0] == nxt[0] and len(part) <= (len(nxt) + 1) // 2:
        return True
    return _edit_distance_within(part, nxt, max(1, (len(nxt) + 1) // 2))


def _collapse_false_start_parts(parts: list[str]) -> list[str]:
    """Collapse hyphenated speech false starts like `po-podemos`, `a-ahora`,
    `altas-atlassian`.

    Single-letter prefixes are accepted now (Spanish speakers produce them
    constantly). Regular compounds such as `cloud-code` or `dm-setters`
    still survive because their first part is not a near-prefix of the
    second (`cloud` vs. `code` are unrelated; `dm` vs. `setters` likewise).
    """
    collapsed: list[str] = []
    for i, part in enumerate(parts):
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        if nxt and _is_false_start_fragment(part, nxt):
            continue
        collapsed.append(part)
    return collapsed


def source_match_sequences(token: str) -> list[tuple[str, ...]]:
    """Return possible final-unit sequences explained by one source word."""
    base = norm(token)
    if not base:
        return []

    parts = alignment_units_for_token(token)
    sequences: list[tuple[str, ...]] = [tuple(parts)]

    if "-" in base and len(parts) > 1:
        collapsed = _collapse_false_start_parts(parts)
        if collapsed != parts:
            sequences.append(tuple(collapsed))

    # Truncated-tail recovery. A Scribe token like `proyecto--` is an
    # abandoned mid-word — `is_truncated()` returns True for it. Today the
    # DP treats truncated tokens as unmatchable (src_sequences = []). But
    # the editor frequently keeps the salvageable stem (e.g. writes
    # `proyecto` after deleting the `--`), and we want that to align
    # against the same audio token. Offer the stem (with trailing dashes
    # / dots stripped) as an alternative sequence. The DP can still skip
    # the source word entirely if the editor chose to drop it; this just
    # adds the "keep the stem" path as a legal match.
    if "--" in token or token.endswith(".."):
        stem = re.sub(r"[-.]+$", "", token)
        stem_norm = norm(stem)
        if stem_norm:
            stem_parts = [p for p in re.split(r"[.\-/]+", stem_norm) if p] or [stem_norm]
            if tuple(stem_parts) not in (tuple(parts),):
                sequences.append(tuple(stem_parts))

    return _dedupe_sequences(sequences)


def is_truncated(token: str) -> bool:
    """Tokens like 'aplicac--' or 'Eh..' or 'poten--' are abandoned mid-word.

    `--` anywhere → truncated.
    `..` at the end → truncated, BUT `...` (ellipsis) is legit punctuation.
    """
    if "--" in token:
        return True
    if token.endswith("..") and not token.endswith("..."):
        return True
    return False


def load_source_words(wfw_path: Path) -> list[dict]:
    """Return list of {text, start, end} from Scribe JSON, in order, only type=word/audio_event."""
    data = json.loads(wfw_path.read_text(encoding="utf-8"))
    out = []
    for w in data["words"]:
        t = w.get("type")
        if t in ("word", "audio_event") and w.get("start") is not None:
            out.append({
                "text": w.get("text", ""),
                "start": float(w["start"]),
                "end": float(w.get("end", w["start"])),
            })
    return out


def tokenize_final(final_text: str) -> list[str]:
    """Split final.txt into source-like tokens.

    Transcribers can emit compounds as adjacent timed tokens, e.g. ``DM`` +
    ``-setters`` or ``School`` + ``.com``, while the plain transcript text
    naturally contains ``DM-setters`` and ``School.com`` as one whitespace
    token. Split those internal punctuation joins so deletion-only editor
    output can still align to the timed word list. Preserve slash-containing
    URL/path tokens because Scribe can return them as one timed token.
    """
    out: list[str] = []
    for token in final_text.split():
        if "/" in token:
            out.append(token)
            continue
        out.extend(re.findall(r"[^.-]+|[.-][^.-]+", token))
    return out


MIN_SPLIT_PART_LEN = 3


def _proportional_split_source_word(w: dict, parts: list[str]) -> list[dict]:
    """Replace one source word with N virtual words covering proportional
    slices of its [start, end] interval. Used to recover from Scribe's
    occasional habit of merging adjacent spoken words into a single timed
    token (e.g. `entornoscerrados` → `entornos` + `cerrados`).

    Piece timestamps are rounded to 3 decimals to match the precision
    `_make_segment` uses when it rounds segment endpoints — without this,
    downstream lookups like `detect_tail_duplicates.src_idx_range()`
    that compare floats with `abs(x - seg["start"]) < 1e-6` would silently
    fail to find the virtual word and return `None`, crashing on the
    next arithmetic operation.
    """
    total_chars = sum(len(p) for p in parts) or 1
    start = float(w["start"])
    end = float(w["end"])
    duration = max(end - start, 0.0)
    out: list[dict] = []
    acc = 0
    for idx, part in enumerate(parts):
        piece_start = start + (acc / total_chars) * duration
        acc += len(part)
        is_last = idx == len(parts) - 1
        piece_end = end if is_last else start + (acc / total_chars) * duration
        out.append({
            "text": part,
            "start": round(piece_start, 3),
            "end": round(piece_end, 3),
        })
    return out


def _try_split_concatenated(
    word_norm: str, fin_units: set[str]
) -> list[str] | None:
    """If `word_norm` looks like 2 or 3 consecutive final tokens glued
    together, return the split parts; otherwise return None.

    Every part must (a) belong to `fin_units` (i.e. actually appear as a
    normalized editor token somewhere) and (b) be at least
    `MIN_SPLIT_PART_LEN` characters long. The length floor protects against
    trivial single-letter cuts that would let any long source word be
    "explained" by an arbitrary sequence of short common final tokens.
    """
    if len(word_norm) < 2 * MIN_SPLIT_PART_LEN:
        return None
    # 2-way split first (cheaper and the common case).
    for k in range(MIN_SPLIT_PART_LEN, len(word_norm) - MIN_SPLIT_PART_LEN + 1):
        a, b = word_norm[:k], word_norm[k:]
        if a in fin_units and b in fin_units:
            return [a, b]
    if len(word_norm) < 3 * MIN_SPLIT_PART_LEN:
        return None
    # 3-way split as a fallback for triple-concatenations.
    for k1 in range(
        MIN_SPLIT_PART_LEN, len(word_norm) - 2 * MIN_SPLIT_PART_LEN + 1
    ):
        for k2 in range(k1 + MIN_SPLIT_PART_LEN, len(word_norm) - MIN_SPLIT_PART_LEN + 1):
            a, b, c = word_norm[:k1], word_norm[k1:k2], word_norm[k2:]
            if a in fin_units and b in fin_units and c in fin_units:
                return [a, b, c]
    return None


def split_concatenated_source_words(
    source_words: list[dict], fin_norm: list[str]
) -> list[dict]:
    """Source-side preprocessing: replace any Scribe token whose normalized
    form is missing from the editor's final tokens but can be decomposed into
    consecutive final-token units with a virtual sequence of split tokens.

    Solves the symmetric case to `tokenize_final`. tokenize_final splits final
    compounds on internal punctuation so they can match per-word Scribe
    tokens; this function splits Scribe tokens that Scribe itself failed to
    separate so they can match per-word editor tokens. Without it the DP
    alignment dies on `entornoscerrados` / `responderlasbien`-style Scribe
    glitches even though every editor word is fully present in the audio.
    """
    fin_units: set[str] = {u for u in fin_norm if u}
    if not fin_units:
        return source_words
    out: list[dict] = []
    for w in source_words:
        base = norm(w.get("text", ""))
        if not base or base in fin_units:
            out.append(w)
            continue
        parts = _try_split_concatenated(base, fin_units)
        if parts is None:
            out.append(w)
            continue
        out.extend(_proportional_split_source_word(w, parts))
    return out


def align(
    source_words: list[dict], final_tokens: list[str]
) -> tuple[list[int], list[dict]]:
    """Return (keep_indices, effective_source_words).

    `keep_indices` is the list of indices that are KEEP in the SAME list
    of source words this function used for alignment — which may differ
    from the caller's input if a Scribe-side concat-split fired. The
    caller MUST use the returned `effective_source_words` (not the input
    list) for every downstream step — `validate`, `build_segments`,
    `inherit_punctuation_status`, EDL emission — or `keep_indices` won't
    index into the right list and downstream sanity checks will trip on
    the unsplit Scribe token (`entornosCerrados` can't "explain"
    `entornos`+`cerrados` because the validator sees the un-split source
    while the DP scored against the split version).

    Final tokens that normalize to empty string (pure punctuation/symbols
    like '%') are dropped before alignment — they carry no matching
    signal.
    """
    # Split final compounds into the same alignment units source candidates use.
    fin_norm = final_alignment_units(final_tokens)

    # Recover from Scribe-side word merges before computing source sequences:
    # source words whose norm is missing from the editor's units but split
    # cleanly into 2-3 final units get replaced with virtual pieces that
    # carry proportional timing. Has to happen here (not in the loader) so
    # we can see the editor's tokens to decide which splits are real.
    source_words = split_concatenated_source_words(source_words, fin_norm)

    # Used to be: truncated tokens (`proyecto--`) were force-skipped via
    # empty sequences here. Now `source_match_sequences()` produces a
    # `stem` alternative for those tokens (the salvageable prefix), so we
    # let the DP decide whether to match the stem or skip the source word
    # entirely.
    src_sequences = [
        source_match_sequences(w["text"]) for w in source_words
    ]

    N = len(source_words)
    M = len(fin_norm)

    NEG = -10**9

    # Two DP tables: state 0 = arrived via skip (skip-source OR skip-final);
    # state 1 = arrived via match. Plus backpointer tables.
    # int32 is plenty: real scores are bounded by ~|SKIP_FINAL|*M + N on the
    # negative side and ~(MATCH+CONTINUITY)*M on the positive side — well
    # under 2**31 for any transcript — and NEG itself fits (the
    # `best_prev <= NEG` guard means two NEGs are never added). Halves the
    # DP memory footprint vs int64.
    dp = np.full((N + 1, M + 1, 2), NEG, dtype=np.int32)
    bp = np.zeros((N + 1, M + 1, 2), dtype=np.int8)
    bp_len = np.zeros((N + 1, M + 1, 2), dtype=np.int16)
    # bp encoding (which prior state we arrived from):
    #   0 = no predecessor (only at start)
    #   1 = came from state 0 at the predecessor cell
    #   2 = came from state 1 at the predecessor cell
    # bp_len encoding for state 0 (disambiguates skip-source vs. skip-final):
    #     0 → skip-source: predecessor cell is (i-1, j)
    #    -1 → skip-final:  predecessor cell is (i, j-1)
    # bp_len for state 1 = k, the number of final units consumed in the match.

    # Base case
    dp[0][0][0] = 0  # state 0 = "last action was skip" (vacuous at start, allows starting either way)

    SKIP_S = SCORING["SKIP_SOURCE"]
    SKIP_F = SCORING["SKIP_FINAL"]
    MATCH = SCORING["MATCH"]
    CONT = SCORING["CONTINUITY"]
    NEW_KEEP = SCORING["START_NEW_KEEP"]
    COMMON_PEN = SCORING["ISOLATED_COMMON_WORD_PENALTY"]

    # Outer loop now extends to N (inclusive) so the DP can still emit
    # skip-final transitions even after every source word has been
    # consumed — needed when the unalignable tail is after the last match.
    for i in range(N + 1):
        for j in range(M + 1):
            best_prev = max(dp[i][j][0], dp[i][j][1])
            if best_prev <= NEG:
                continue

            # Transition 1: skip source word S[i] (only valid while i < N).
            # Truncated tokens get a smaller skip cost (we WANT to skip
            # them); but standard skip is fine — just don't bonus them.
            if i < N:
                skip_cost = SKIP_S
                cand = best_prev + skip_cost
                if cand > dp[i + 1][j][0]:
                    dp[i + 1][j][0] = cand
                    bp[i + 1][j][0] = 1 if dp[i][j][0] >= dp[i][j][1] else 2
                    bp_len[i + 1][j][0] = 0  # sentinel: skip-source

            # Transition 2: match S[i] → T[j], if normalized equal and the
            # source word is not truncated. Tiny/zero-duration words can still
            # be required text anchors; timing cleanup happens after alignment.
            if i < N and j < M:
                for seq in src_sequences[i]:
                    k = len(seq)
                    if j + k > M or tuple(fin_norm[j : j + k]) != seq:
                        continue
                    came_from_match = dp[i][j][1] >= dp[i][j][0]
                    base = dp[i][j][1] if came_from_match else dp[i][j][0]
                    add = (MATCH * k) + (CONT if came_from_match else NEW_KEEP)
                    # Isolated common-word penalty: if starting new keep run AND it's a common word,
                    # add extra penalty so single-word islands of "y", "el", etc. are unprofitable.
                    if not came_from_match and k == 1 and seq[0] in COMMON_WORDS:
                        add += COMMON_PEN
                    cand = base + add
                    if cand > dp[i + 1][j + k][1]:
                        dp[i + 1][j + k][1] = cand
                        bp[i + 1][j + k][1] = 2 if came_from_match else 1
                        bp_len[i + 1][j + k][1] = k

            # Transition 3: skip final token T[j] (advance j without
            # touching i). Carries a heavy `SKIP_FINAL` penalty so the DP
            # only chooses it when no match is available. After the DP
            # completes we walk the backtrack and collect every j that
            # took this path — the full inventory of positions the
            # Finalizer has to delete in a single pass.
            if j < M:
                cand = best_prev + SKIP_F
                if cand > dp[i][j + 1][0]:
                    dp[i][j + 1][0] = cand
                    bp[i][j + 1][0] = 1 if dp[i][j][0] >= dp[i][j][1] else 2
                    bp_len[i][j + 1][0] = -1  # sentinel: skip-final

    # With the skip-final transition added, the DP can always reach (N, M)
    # by paying SKIP_FINAL per unmatchable final token. The "alignment
    # success" criterion therefore becomes "the optimal path used zero
    # skip-final steps" rather than "the DP could reach the end at all".
    end_score = max(dp[N][M][0], dp[N][M][1])
    if end_score <= NEG:
        # Truly degenerate: even with skip-final the DP has no path. Fall
        # back to the legacy frontier-only failure mode.
        frontier = _compute_alignment_frontier(
            dp, N, M, NEG, source_words, final_tokens, fin_norm
        )
        src_set = {unit for sequences in src_sequences for seq in sequences for unit in seq}
        missing = sorted({n for n in fin_norm if n and n not in src_set})
        diagnostics = {
            "mode": "dp_unreachable",
            "issues": ["dp_unreachable"],
            "total_source_words": N,
            "total_final_tokens": M,
            "missing_count": len(missing),
            "missing_sample": missing[:32],
            "missing_all": missing,
            "frontier": frontier,
        }
        raise AlignmentFailure(
            (
                f"DP alignment failed catastrophically: no path reaches "
                f"({N}, {M}) even with skip-final allowed. Check mapper "
                f"scoring constants."
            ),
            diagnostics,
        )
    end_state = 1 if dp[N][M][1] >= dp[N][M][0] else 0

    # Backtrack. bp[i][j][s] encodes which prior-cell state we came from:
    #   1 = prev cell was state 0
    #   2 = prev cell was state 1
    # bp_len[i][j][s] disambiguates the action at the current step:
    #   s == 0 and bp_len ==  0 → arrived via skip-source from (i-1, j)
    #   s == 0 and bp_len == -1 → arrived via skip-final  from (i, j-1)
    #   s == 1 and bp_len  >  0 → arrived via match       from (i-1, j-k)
    # Each skip-final hit records (j-1), the position of the unmatchable
    # final token. We collect them all to produce the full issue list.
    keep_indices: list[int] = []
    skipped_finals: list[int] = []
    i, j, s = N, M, end_state
    while i > 0 or j > 0:
        prev_marker = bp[i][j][s]
        if prev_marker == 0:
            # No predecessor recorded — shouldn't happen below the origin.
            break
        if s == 0:
            length = int(bp_len[i][j][s])
            if length == 0:
                # skip-source
                i -= 1
            elif length == -1:
                # skip-final
                if j <= 0:
                    break
                skipped_finals.append(j - 1)
                j -= 1
            else:
                break
        else:
            consumed = int(bp_len[i][j][s])
            if consumed <= 0:
                break
            keep_indices.append(i - 1)
            i -= 1
            j -= consumed
        s = 0 if prev_marker == 1 else 1

    keep_indices.reverse()

    if not skipped_finals:
        return keep_indices, source_words

    # We had unmatched final tokens — assemble the full issue report so
    # the Finalizer can delete every position in a single Claude call.
    skipped_finals.sort()
    skipped_ranges: list[dict] = []
    range_start = skipped_finals[0]
    range_prev = range_start
    for fj in skipped_finals[1:]:
        if fj == range_prev + 1:
            range_prev = fj
        else:
            skipped_ranges.append(
                _build_skipped_range(range_start, range_prev, final_tokens, fin_norm)
            )
            range_start = fj
            range_prev = fj
    skipped_ranges.append(
        _build_skipped_range(range_start, range_prev, final_tokens, fin_norm)
    )

    src_set = {unit for sequences in src_sequences for seq in sequences for unit in seq}
    missing = sorted({n for n in fin_norm if n and n not in src_set})
    skipped_norms = {fin_norm[fj] for fj in skipped_finals}
    missing_inside_skips = sorted(skipped_norms & set(missing))
    reorder_only_skips = sorted(skipped_norms - set(missing))

    issues_summary: list[str] = []
    if missing:
        issues_summary.append("missing_words")
    if reorder_only_skips:
        issues_summary.append("reorder_unalignable")
    diagnostics = {
        "mode": (
            "missing_words+reorder"
            if missing and reorder_only_skips
            else ("missing_words" if missing else "reorder_unalignable")
        ),
        "issues": issues_summary,
        "total_source_words": N,
        "total_final_tokens": M,
        "missing_count": len(missing),
        "missing_sample": missing[:32],
        "missing_all": missing,
        # Every position the Finalizer must delete (or include in a
        # whole-sentence deletion). Each entry has the contiguous range
        # plus a short token snippet so the agent can locate it without
        # counting indices by hand.
        "skipped_final_count": len(skipped_finals),
        "skipped_finals": skipped_finals,
        "skipped_ranges": skipped_ranges,
        "missing_inside_skips": missing_inside_skips,
        "reorder_only_skips": reorder_only_skips,
        # Back-compat: keep a `frontier` field set to the FIRST skipped
        # range so existing log readers / Finalizer prompts that look for
        # `furthest_final_idx` etc. still find something useful.
        "frontier": _frontier_from_first_skip(
            skipped_ranges[0], source_words, final_tokens, fin_norm
        ),
    }
    message_parts = [
        f"DP alignment failed: {len(skipped_finals)} final token(s) across "
        f"{len(skipped_ranges)} contiguous range(s) cannot be matched in "
        f"order against the source transcript."
    ]
    if missing:
        preview = ", ".join(missing[:8])
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        message_parts.append(
            f"{len(missing)} of these are missing-word issues "
            f"(token not in source at all): [{preview}]{more}."
        )
    if reorder_only_skips:
        message_parts.append(
            f"{len(reorder_only_skips)} additional issue(s) are reorder "
            f"failures (token exists in source but cannot align here)."
        )
    raise AlignmentFailure(" ".join(message_parts), diagnostics)


def _build_runs(keep_set: set[int]) -> list[list[int]]:
    keeps = sorted(keep_set)
    if not keeps:
        return []
    runs: list[list[int]] = []
    current = [keeps[0]]
    for k in keeps[1:]:
        if k == current[-1] + 1:
            current.append(k)
        else:
            runs.append(current)
            current = [k]
    runs.append(current)
    return runs


def fix_tail_residuals(
    keep_indices: list[int], source_words: list[dict], max_tail_words: int = 10
) -> list[int]:
    """Iteratively detect and repair tail-residuals.

    A tail-residual is a keep run A whose last K words are duplicated as the
    K source words immediately preceding the next keep run B. The DP often
    prefers matching those K words "early" (via continuity bonus from A) rather
    than "late" (as the start of B). This pass moves them so the match comes
    from the position contiguous with B.

    Algorithm: while there is any pair (A, B) where A's largest matching suffix
    against the words immediately before B[0] is non-zero, swap those words.
    Re-build runs after each swap and re-scan from the start. Iterates until
    a full scan finds no more swaps.

    `max_tail_words` caps the suffix length tried per pass. Iteration handles
    longer suffixes via multiple passes; cap exists for runtime safety.
    """
    if not keep_indices:
        return keep_indices

    src_norm = [norm(w["text"]) for w in source_words]
    src_truncated = [is_truncated(w["text"]) for w in source_words]
    src_matchable = [
        bool(src_norm[i]) and not src_truncated[i]
        for i in range(len(source_words))
    ]

    keep_set = set(keep_indices)

    while True:
        changed = False
        runs = _build_runs(keep_set)
        for i in range(len(runs) - 1):
            run_a = runs[i]
            run_b = runs[i + 1]
            cap = min(max_tail_words, len(run_a), run_b[0])
            best_size = 0
            for size in range(cap, 0, -1):
                tail_a = run_a[-size:]
                cand = list(range(run_b[0] - size, run_b[0]))
                # Validity: candidates must be normal tokens. Duration is not a
                # text-alignment signal; later EDL validation handles timing.
                if any(not src_matchable[k] for k in cand):
                    continue
                if any(not src_matchable[k] for k in tail_a):
                    continue
                if [src_norm[k] for k in tail_a] == [src_norm[k] for k in cand]:
                    best_size = size
                    break
            if best_size:
                tail_a = run_a[-best_size:]
                cand = list(range(run_b[0] - best_size, run_b[0]))
                for k in tail_a:
                    keep_set.discard(k)
                for k in cand:
                    keep_set.add(k)
                changed = True
                break  # re-scan from start with rebuilt runs
        if not changed:
            break

    return sorted(keep_set)


def inherit_punctuation_status(
    source_words: list[dict], keep_set: set[int]
) -> set[int]:
    """Source tokens that normalize to empty (pure symbols like '%', stray ','
    or '.') cannot match anything in the DP and end up as CUT by default. If
    such a token sits between two KEEP words, its CUT status creates a tiny
    gap that strips the symbol from the output (e.g., '99 %' → '99' only).

    Fix: any source token whose norm is empty inherits the keep/cut status of
    its preceding source token. The token attaches to whatever run came before.
    """
    new_keep_set = set(keep_set)
    for i, w in enumerate(source_words):
        if norm(w["text"]):
            continue
        if i == 0:
            continue
        if (i - 1) in new_keep_set:
            new_keep_set.add(i)
    return new_keep_set


def build_segments(source_words: list[dict], keep_set: set[int]) -> list[dict]:
    """Group consecutive source words into segments by their keep/cut status.

    Coverage: contiguous, no gaps. Each segment has start (first word.start),
    end (last word.end), status, text.
    """
    segments: list[dict] = []
    if not source_words:
        return segments
    cur_status = "keep" if 0 in keep_set else "cut"
    cur_start_idx = 0
    for i in range(1, len(source_words)):
        status = "keep" if i in keep_set else "cut"
        if status != cur_status:
            segments.append(_make_segment(source_words, cur_start_idx, i - 1, cur_status))
            cur_status = status
            cur_start_idx = i
    segments.append(_make_segment(source_words, cur_start_idx, len(source_words) - 1, cur_status))
    return segments


def _make_segment(words: list[dict], i: int, j: int, status: str) -> dict:
    text = " ".join(w["text"] for w in words[i : j + 1])
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "start": round(words[i]["start"], 3),
        "end": round(words[j]["end"], 3),
        "status": status,
        "text": text,
    }


def detect_tail_duplicates(
    segments: list[dict], source_words: list[dict]
) -> tuple[list[str], list[str]]:
    """Detect any keep segment whose trailing K words (norm-equal) are duplicated
    as the K source words immediately preceding the next keep segment.

    If matched, classify by severity:
      - error: the duplicated phrase is a connector (asi que, ahora bien, ...).
        These are extremely suspicious as keep tails — almost certainly a residual
        the repair pass missed.
      - warning: any other duplicated phrase.
    """
    src_norm = [norm(w["text"]) for w in source_words]
    src_truncated = [is_truncated(w["text"]) for w in source_words]
    src_matchable = [
        bool(src_norm[i]) and not src_truncated[i]
        for i in range(len(source_words))
    ]

    # Build mapping from segment to (first_src_idx, last_src_idx)
    keep_segs = [(i, s) for i, s in enumerate(segments) if s["status"] == "keep"]

    # Locate src indices spanned by each keep segment using start/end timestamps
    def src_idx_range(seg):
        first = next(
            (k for k, w in enumerate(source_words)
             if abs(float(w["start"]) - seg["start"]) < 1e-6),
            None,
        )
        last = next(
            (k for k, w in enumerate(source_words)
             if abs(float(w["end"]) - seg["end"]) < 1e-6),
            None,
        )
        return first, last

    errors: list[str] = []
    warnings: list[str] = []

    for n in range(len(keep_segs) - 1):
        a_seg = keep_segs[n][1]
        b_seg = keep_segs[n + 1][1]
        a_first, a_last = src_idx_range(a_seg)
        b_first, _ = src_idx_range(b_seg)
        if a_first is None or a_last is None or b_first is None:
            # Pre-existing edge case (one of the segment endpoints couldn't
            # be located via float-equality lookup) — surfaced by virtual
            # source words from `split_concatenated_source_words` whose
            # boundaries don't exactly coincide with any original token.
            # Skip the check for this pair; tail-residual detection here is
            # advisory, not load-bearing.
            continue
        # Find largest matching suffix of A in source preceding B[0]
        cap = min(TAIL_DUP_MAX_LOOKBACK, a_last - a_first + 1, b_first)
        matched_size = 0
        for size in range(cap, 0, -1):
            tail = list(range(a_last - size + 1, a_last + 1))
            cand = list(range(b_first - size, b_first))
            if any(not src_matchable[k] for k in cand + tail):
                continue
            if [src_norm[k] for k in tail] == [src_norm[k] for k in cand]:
                matched_size = size
                break
        if matched_size:
            phrase = " ".join(src_norm[k] for k in range(a_last - matched_size + 1, a_last + 1))
            msg = (
                f"keep [{a_seg['start']:.2f}-{a_seg['end']:.2f}] tail "
                f"\"{phrase}\" is duplicated immediately before next keep "
                f"[{b_seg['start']:.2f}-{b_seg['end']:.2f}]"
            )
            if phrase in CONNECTOR_PHRASES:
                errors.append("connector tail residual: " + msg)
            else:
                warnings.append("tail residual: " + msg)
    return errors, warnings


def kept_source_explains_final(
    source_words: list[dict], keep_set: set[int], final_units: list[str]
) -> tuple[bool, str]:
    positions: set[int] = {0}
    for i, word in enumerate(source_words):
        if i not in keep_set:
            continue
        # `is_truncated()` no longer disqualifies the source word — the
        # DP can now match against a truncated token's stem (e.g. keep
        # `proyecto` from source `proyecto--`). The post-validation just
        # walks the same `source_match_sequences()` candidates and accepts
        # whichever one the DP committed to.

        sequences = source_match_sequences(word["text"])
        if not sequences:
            continue

        next_positions: set[int] = set()
        for pos in positions:
            for seq in sequences:
                end = pos + len(seq)
                if end <= len(final_units) and tuple(final_units[pos:end]) == seq:
                    next_positions.add(end)
        if not next_positions:
            pos = min(positions) if positions else 0
            final_context = " ".join(final_units[max(0, pos - 6) : min(len(final_units), pos + 7)])
            return (
                False,
                f"kept source word #{i} cannot explain final alignment units near #{pos}: "
                f"source=\"{word['text']}\" final_context=\"{final_context}\"",
            )
        positions = next_positions

    if len(final_units) not in positions:
        pos = max(positions) if positions else 0
        final_context = " ".join(final_units[max(0, pos - 6) : min(len(final_units), pos + 7)])
        return (
            False,
            f"mapped source did not consume all final alignment units "
            f"(consumed up to #{pos}, final={len(final_units)}): \"{final_context}\"",
        )
    return True, ""


def validate(
    segments: list[dict],
    final_tokens: list[str],
    source_words: list[dict],
    keep_set: set[int],
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block; warnings are informational."""
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Reconstruct mapped final from kept source words using the same
    # compound alignment units as the DP.
    final_units = final_alignment_units(final_tokens)
    ok, msg = kept_source_explains_final(source_words, keep_set, final_units)
    if not ok:
        errors.append(msg)

    # 2. Per-keep-segment soft checks
    keep_segs = [s for s in segments if s["status"] == "keep"]
    for idx, seg in enumerate(keep_segs):
        dur = seg["end"] - seg["start"]
        n_words = len(seg["text"].split())
        is_edge = idx == 0 or idx == len(keep_segs) - 1
        if dur < VAL_MIN_KEEP_SECONDS and not is_edge:
            warnings.append(
                f"keep segment [{seg['start']:.2f}-{seg['end']:.2f}] only {dur:.2f}s "
                f"(<{VAL_MIN_KEEP_SECONDS}s): \"{seg['text'][:60]}\""
            )
        if n_words < VAL_MIN_KEEP_WORDS and not is_edge:
            warnings.append(
                f"keep segment [{seg['start']:.2f}-{seg['end']:.2f}] only {n_words} word(s) "
                f"(<{VAL_MIN_KEEP_WORDS}): \"{seg['text']}\""
            )

    # 3. Hook retention: a first keep range that starts deep into the source
    # usually means the opening hook was dropped by the cut.
    if keep_segs and keep_segs[0]["start"] > HOOK_START_WARN_SECONDS:
        warnings.append(
            f"first keep range starts at {keep_segs[0]['start']:.2f}s of the "
            f"source (>{HOOK_START_WARN_SECONDS:.0f}s) — the opening hook was "
            f"likely dropped; check that the editor kept the intro"
        )

    # 4. Tiny inter-keep gaps (cuts shorter than threshold between two keeps)
    for i in range(1, len(segments) - 1):
        seg = segments[i]
        if seg["status"] != "cut":
            continue
        prev = segments[i - 1]
        nxt = segments[i + 1]
        if prev["status"] != "keep" or nxt["status"] != "keep":
            continue
        gap = seg["end"] - seg["start"]
        if gap < VAL_TINY_GAP_SECONDS:
            warnings.append(
                f"tiny cut gap {gap*1000:.0f}ms at [{seg['start']:.2f}-{seg['end']:.2f}]: "
                f"\"{seg['text']}\""
            )

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic mapper — aligns approved final.txt against word_for_word.json and emits edl.json."
    )
    ap.add_argument("final_txt", type=Path)
    ap.add_argument("word_for_word_json", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--source-name", type=str, default=None,
                    help="Source identifier in the EDL (default: stem of word_for_word.json)")
    ap.add_argument("--strict", action="store_true",
                    help="Treat warnings as errors")
    args = ap.parse_args()

    final_text = args.final_txt.read_text(encoding="utf-8-sig")
    final_tokens = tokenize_final(final_text)
    final_units = final_alignment_units(final_tokens)
    source_words = load_source_words(args.word_for_word_json)

    print(f"source words: {len(source_words)}")
    print(f"final tokens: {len(final_tokens)}")
    if len(final_units) != len([norm(t) for t in final_tokens if norm(t)]):
        print(f"final alignment tokens: {len(final_units)}")

    # Every failure path below MUST write a fresh error log to disk before
    # exiting. The orchestrator's Finalizer-recovery loop snapshots the
    # log dir before each mapper call and demands a NEW log afterward — if
    # no log appears it refuses to call the Finalizer (which would
    # otherwise receive a stale diagnostic from a previous failure and
    # destroy the transcript by trying to "fix" issues that no longer
    # exist).
    def _log_and_exit(err: BaseException, *, exit_code: int, reraise: bool) -> int:
        log_path = _write_error_log(
            args.output,
            args.final_txt,
            args.word_for_word_json,
            err,
            extra={
                "source words": len(source_words),
                "final tokens": len(final_tokens),
            },
        )
        print(f"\nERROR: {err}", file=sys.stderr)
        print(f"Wrote detailed mapper error log: {log_path}", file=sys.stderr)
        if reraise:
            raise err
        return exit_code

    try:
        keep_indices, source_words = align(source_words, final_tokens)
    except AlignmentFailure as err:
        return _log_and_exit(err, exit_code=2, reraise=False)
    except Exception as err:  # noqa: BLE001
        return _log_and_exit(err, exit_code=2, reraise=True)

    try:
        keep_indices_repaired = fix_tail_residuals(keep_indices, source_words)
        keep_indices_before = set(keep_indices)
        n_repaired = sum(1 for k in keep_indices_repaired if k not in keep_indices_before)
        if n_repaired:
            print(f"tail-residual repair: moved {n_repaired} word(s) to later runs")
        keep_set = set(keep_indices_repaired)
        keep_set_with_punct = inherit_punctuation_status(source_words, keep_set)
        n_punct = len(keep_set_with_punct) - len(keep_set)
        if n_punct:
            print(f"punctuation inheritance: attached {n_punct} symbol token(s) to runs")
        keep_set = keep_set_with_punct

        print(f"keep words: {len(keep_set)}")
        print(f"cut words:  {len(source_words) - len(keep_set)}")

        segments = build_segments(source_words, keep_set)
        keep_segs = [s for s in segments if s["status"] == "keep"]
        cut_segs = [s for s in segments if s["status"] == "cut"]
        keep_seconds = sum(s["end"] - s["start"] for s in keep_segs)
        cut_seconds = sum(s["end"] - s["start"] for s in cut_segs)

        print(f"segments: {len(segments)} (keep: {len(keep_segs)}, cut: {len(cut_segs)})")
        print(f"  keep: {keep_seconds:.2f}s ({keep_seconds/60:.2f}m)")
        print(f"  cut:  {cut_seconds:.2f}s ({cut_seconds/60:.2f}m)")

        # Loud, standalone banner (in addition to the validate() warning): a
        # dropped hook is the single most damaging silent editing mistake for
        # a short, so it must not hide inside the ordinary warning list.
        if keep_segs and keep_segs[0]["start"] > HOOK_START_WARN_SECONDS:
            banner = "!" * 72
            print(
                f"\n{banner}\n"
                f"!! WARNING: first keep range starts at "
                f"{keep_segs[0]['start']:.2f}s of the source "
                f"(>{HOOK_START_WARN_SECONDS:.0f}s).\n"
                f"!! The opening hook was likely dropped by the cut — review "
                f"the edit.\n"
                f"{banner}",
                flush=True,
            )

        errors, warnings = validate(segments, final_tokens, source_words, keep_set)
        td_errors, td_warnings = detect_tail_duplicates(segments, source_words)
        errors.extend(td_errors)
        warnings.extend(td_warnings)
        if warnings:
            print(f"\n{len(warnings)} warning(s):")
            for w in warnings:
                print(f"  WARN: {w}")
        if errors:
            print(f"\n{len(errors)} error(s):")
            for e in errors:
                print(f"  ERR:  {e}")
            # Raise ValidatorFailure so the unified catch below writes a
            # fresh error log. This keeps the "every failure writes a log"
            # invariant the orchestrator relies on for its recovery loop.
            raise ValidatorFailure(
                "Post-DP mapper validator rejected the alignment.",
                errors=errors,
                warnings=warnings,
            )

        if args.strict and warnings:
            print("\n--strict: warnings treated as errors")
            raise ValidatorFailure(
                "--strict mode: warnings escalated to errors.",
                errors=[f"strict: {w}" for w in warnings],
                warnings=[],
            )

        source_name = args.source_name or args.word_for_word_json.stem

        edl = {
            "source": source_name,
            "total_words_verbose": len(source_words),
            "total_words_edited": len(final_tokens),
            "n_segments": len(segments),
            "n_keep_segments": len(keep_segs),
            "n_cut_segments": len(cut_segs),
            "total_keep_seconds": round(keep_seconds, 3),
            "total_cut_seconds": round(cut_seconds, 3),
            "segments": segments,
        }

        args.output.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {args.output}")
        return 0
    except ValidatorFailure as err:
        return _log_and_exit(err, exit_code=2, reraise=False)
    except Exception as err:  # noqa: BLE001
        return _log_and_exit(err, exit_code=2, reraise=True)


if __name__ == "__main__":
    sys.exit(main())
