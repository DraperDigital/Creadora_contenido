"""Deterministic detector for consecutive duplicate n-grams (verbatim stutters).

Catches the class of editor failure where the same word sequence appears
back-to-back in `final.txt` (e.g. "Y en caso de que, y en caso de que se te
olvide"). The editor + reviewer are LLM-judged and can miss literal stutters
that this validator finds in O(n*k) by exact comparison after normalization.

Two passes:
- Exact pass (`find_stutters`): back-to-back duplicate n-grams of 2+ words.
  1-word repeats are deliberately NOT flagged — rhetorical doubling like
  "muy muy" is legitimate speech. Exact findings are a hard reject.
- Fuzzy pass (`find_fuzzy_stutters`): back-to-back n-grams that are equal
  except for exactly ONE token (usually two takes of the same sentence with
  a single changed word). Fuzzy findings are ADVISORY only — the runner
  emits them as feedback to the next editor iteration, never a hard reject.

Usage:
    python stutter_check.py <final.txt>
        exit 0 + "OK" on stdout if no exact stutters (fuzzy findings, if
        any, are printed as advisory).
        exit 1 + findings on stdout otherwise.

Findings have shape:
    {"start_token": int, "ngram_length": int, "text": str, "context": str}

The runner imports `find_stutters()` / `find_fuzzy_stutters()` and the two
formatters directly so the editor-reviewer loop can act without spawning a
subprocess.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

MIN_NGRAM_LENGTH = 2
MAX_NGRAM_LENGTH = 12
# The fuzzy pass needs at least 2 matching tokens around the single allowed
# difference; below 3 tokens nearly every "de la / de los"-style bigram pair
# would fire and the feedback would be pure noise.
FUZZY_MIN_NGRAM_LENGTH = 3


def _normalize_word(token: str) -> str:
    if not token:
        return ""
    stripped = "".join(
        ch
        for ch in unicodedata.normalize("NFD", token.lower())
        if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"^[^\wáéíóúñü]+|[^\wáéíóúñü]+$", "", stripped)


def _tokenize(text: str) -> tuple[list[str], list[str]]:
    raw = [tok for tok in re.split(r"\s+", text) if tok]
    return raw, [_normalize_word(tok) for tok in raw]


def find_stutters(
    text: str,
    min_ngram: int = MIN_NGRAM_LENGTH,
    max_ngram: int = MAX_NGRAM_LENGTH,
) -> list[dict]:
    """Return every back-to-back duplicate n-gram in `text`.

    Two n-grams are considered duplicate if their per-token normalization
    (lowercase, accent-stripped, edge-punctuation-stripped) matches exactly.
    The detector prefers the longest match starting at each position so a
    5-word stutter is reported once instead of three overlapping 3-grams.
    The floor is 2 words; single-word repeats stay out because rhetorical
    doubling ("muy muy") is legitimate speech, not a stutter.
    """
    raw, norm = _tokenize(text)
    findings: list[dict] = []
    i = 0
    n_tokens = len(norm)
    while i < n_tokens:
        matched = False
        for n in range(max_ngram, min_ngram - 1, -1):
            if i + 2 * n > n_tokens:
                continue
            a = norm[i : i + n]
            b = norm[i + n : i + 2 * n]
            if not all(a) or not all(b):
                continue
            if a != b:
                continue
            ctx_start = max(0, i - 3)
            ctx_end = min(n_tokens, i + 2 * n + 3)
            findings.append(
                {
                    "start_token": i,
                    "ngram_length": n,
                    "text": " ".join(raw[i : i + 2 * n]),
                    "context": " ".join(raw[ctx_start:ctx_end]),
                }
            )
            i += n
            matched = True
            break
        if not matched:
            i += 1
    return findings


def find_fuzzy_stutters(
    text: str,
    min_ngram: int = FUZZY_MIN_NGRAM_LENGTH,
    max_ngram: int = MAX_NGRAM_LENGTH,
) -> list[dict]:
    """Return back-to-back n-gram pairs equal except for exactly ONE token.

    These are typically two takes of the same sentence where the speaker
    changed a single word ("vamos a ver, vamos a hacer") — too ambiguous to
    auto-reject (deliberate parallelism looks the same), so callers emit them
    as ADVISORY feedback to the next editor iteration, never a hard reject.
    Exact duplicates (zero differing tokens) are `find_stutters`' job and are
    skipped here.
    """
    raw, norm = _tokenize(text)
    findings: list[dict] = []
    i = 0
    n_tokens = len(norm)
    while i < n_tokens:
        matched = False
        for n in range(max_ngram, min_ngram - 1, -1):
            if i + 2 * n > n_tokens:
                continue
            a = norm[i : i + n]
            b = norm[i + n : i + 2 * n]
            if not all(a) or not all(b):
                continue
            diffs = sum(1 for x, y in zip(a, b) if x != y)
            if diffs != 1:
                continue
            ctx_start = max(0, i - 3)
            ctx_end = min(n_tokens, i + 2 * n + 3)
            findings.append(
                {
                    "start_token": i,
                    "ngram_length": n,
                    "text": " ".join(raw[i : i + 2 * n]),
                    "context": " ".join(raw[ctx_start:ctx_end]),
                }
            )
            i += 2 * n
            matched = True
            break
        if not matched:
            i += 1
    return findings


def format_findings(findings: list[dict]) -> str:
    """Render findings as reviewer feedback the next editor iteration can act on."""
    if not findings:
        return ""
    lines = [
        f"Se detectaron {len(findings)} tartamudeo(s) consecutivo(s) literal(es) "
        "en la propuesta. Para cada uno, conservar SOLO la última ocurrencia "
        "del n-grama y borrar la(s) anterior(es) junto con cualquier "
        "puntuación intermedia. No reescribir, no reordenar — solo borrar.",
        "",
    ]
    for idx, finding in enumerate(findings, 1):
        lines.append(
            f"{idx}. n-grama de {finding['ngram_length']} palabra(s) "
            f"repetido en posición {finding['start_token']}:"
        )
        lines.append(f"   Repetición literal: {finding['text']!r}")
        lines.append(f"   Contexto: …{finding['context']}…")
    return "\n".join(lines)


def format_fuzzy_findings(findings: list[dict]) -> str:
    """Render near-duplicate findings as ADVISORY feedback for the editor.

    Never phrased as a hard rejection: the editor must re-check each case
    and only delete when it really is two takes of the same idea.
    """
    if not findings:
        return ""
    lines = [
        f"Aviso (no bloqueante): se detectaron {len(findings)} par(es) de "
        "n-gramas consecutivos CASI idénticos (difieren en una sola palabra). "
        "Suelen ser dos tomas de la misma idea. Revisar cada caso: si son dos "
        "tomas, conservar SOLO la última y borrar la anterior; si es una "
        "estructura retórica intencional del hablante, dejarla como está. No "
        "reescribir, no reordenar — solo borrar cuando corresponda.",
        "",
    ]
    for idx, finding in enumerate(findings, 1):
        lines.append(
            f"{idx}. n-grama de {finding['ngram_length']} palabra(s) casi "
            f"repetido en posición {finding['start_token']}:"
        )
        lines.append(f"   Texto: {finding['text']!r}")
        lines.append(f"   Contexto: …{finding['context']}…")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to final.txt or editor proposal")
    parser.add_argument(
        "--min-ngram",
        type=int,
        default=MIN_NGRAM_LENGTH,
        help=f"Minimum n-gram length to flag (default: {MIN_NGRAM_LENGTH})",
    )
    parser.add_argument(
        "--max-ngram",
        type=int,
        default=MAX_NGRAM_LENGTH,
        help=f"Maximum n-gram length to consider (default: {MAX_NGRAM_LENGTH})",
    )
    parser.add_argument(
        "--format",
        choices=["json", "human"],
        default="human",
        help="Output format when stutters are found",
    )
    args = parser.parse_args()
    if not args.path.exists():
        print(f"ERROR: file not found: {args.path}", file=sys.stderr)
        return 2
    text = args.path.read_text(encoding="utf-8-sig")
    findings = find_stutters(text, min_ngram=args.min_ngram, max_ngram=args.max_ngram)
    fuzzy = find_fuzzy_stutters(text, max_ngram=args.max_ngram)
    if not findings:
        print("OK: no consecutive duplicate n-grams detected")
        if fuzzy:
            # Advisory only — near-duplicates never fail the check.
            print()
            print(format_fuzzy_findings(fuzzy))
        return 0
    if args.format == "json":
        print(json.dumps(findings, ensure_ascii=False, indent=2))
    else:
        print(format_findings(findings))
        if fuzzy:
            print()
            print(format_fuzzy_findings(fuzzy))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
