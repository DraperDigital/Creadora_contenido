"""Pure word -> caption-cue chunker (stdlib only).

Captions are rendered by Remotion (NOT burned ASS). This module turns
word-level timings into single-line caption cues. The chunking logic is
ported faithfully from the old ``short/animate/build_captions_ass.py``
(``_clean_words`` / ``_chunk_words`` / ``_chunk_chars``); the only change
is the output shape: each cue is ``{"text", "start", "end"}`` instead of
an ASS Dialogue event.
"""
from __future__ import annotations

from typing import Any, Sequence


def clean_words(
    words: Sequence[dict[str, Any]],
    duration: float,
) -> list[dict[str, Any]]:
    """Normalize raw word rows to ``[{word, start, end}]``.

    Port of ``build_captions_ass._clean_words``: accept ``word`` or
    ``text``, require float ``start < end``, drop words that start at/after
    ``duration``, and clip ``end`` to ``duration``.
    """
    cleaned: list[dict[str, Any]] = []
    for raw in words:
        if not isinstance(raw, dict):
            continue
        text = (raw.get("word") or raw.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if start >= duration:
            continue
        end = min(end, duration)
        cleaned.append({"word": text, "start": start, "end": end})
    return cleaned


def _chunk_chars(chunk: list[dict[str, Any]]) -> int:
    """Total visible characters of a chunk, including inter-word spaces."""
    if not chunk:
        return 0
    total = sum(len((w.get("word") or "").strip()) for w in chunk)
    return total + max(0, len(chunk) - 1)  # +1 char per inter-word space


# Captions render at a FIXED size (see shared/remotion/src/Captions.tsx
# CAPTION_FONT_PX — keep these in sync). The number of words shown per cue is
# decided HERE: how many characters fit on ONE line at that fixed size within
# the safe width, so a caption never wraps to a 2nd line or clips the edges.
CAPTION_FONT_PX = 88
# SHORT captions are chunked by a fixed CHARACTER cap (fit as many whole words
# as possible within N chars; the overflowing word starts the next cue) and
# render on exactly ONE line at a fixed size. These two constants are a matched
# pair — changing the cap means re-checking the font, and vice-versa.
SHORT_CAPTION_MAX_CHARS = 26
SHORT_CAPTION_FONT_PX = 60
# LONG (16:9, >=1920 wide) captions render 30% smaller than short/vertical ones.
LONG_CAPTION_SCALE = 0.7
# Conservative average glyph advance for a bold sans-serif, as a fraction of the
# font size. Deliberately a slight over-estimate so the fit never overflows.
_CHAR_WIDTH_RATIO = 0.62
_SENTENCE_END = (".", "?", "!", "…")
# Softer clause breaks — preferred split points inside a long sentence so cues
# land on commas/clauses instead of mid-phrase at the word cap.
_CLAUSE_END = (",", ";", ":")


def caption_font_px(canvas_width: int) -> int:
    """Fixed caption size for a canvas. LONG/horizontal (>=1920) is 30% smaller.

    The SAME value must drive the Remotion render (CaptionsProps.fontPx) AND this
    module's word-fit chunking, so the words-per-cue match the rendered size.
    """
    if canvas_width >= 1920:
        return round(CAPTION_FONT_PX * LONG_CAPTION_SCALE)
    return CAPTION_FONT_PX


def _max_chars_for(canvas_width: int, font_px: int, safe_ratio: float) -> int:
    """Max single-line characters that fit at `font_px` within the safe width."""
    usable = canvas_width * safe_ratio
    return max(6, int(usable / (font_px * _CHAR_WIDTH_RATIO)))


def chunk_cues(
    words: Sequence[dict[str, Any]],
    duration: float,
    *,
    canvas_width: int,
    font_px: int = CAPTION_FONT_PX,
    safe_ratio: float = 0.9,
    max_words: int = 9,
    # Default 3 so a cue never breaks on punctuation after one or two words —
    # the anti-one-word-flash guard applies to every pipeline (the short
    # orchestrators also pass 3 explicitly).
    min_words: int = 3,
    max_lines: int = 1,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Break word-level timings into single-line caption cues at a FIXED size.

    Caption text size is constant; what varies is how many words fit on one
    line. ``max_chars`` is DERIVED from ``canvas_width`` + ``font_px`` so each
    cue fits one line within the safe width (no wrap, no edge clipping) — a
    wider canvas simply fits more words. Cues also break at sentence-final
    punctuation, with ``max_words`` as a sanity cap.

    Returns cues ``{"text": " ".join(words), "start": first.start, "end": last.end}``.
    """
    cleaned = clean_words(words, duration)
    # Character budget per cue. When `max_chars` is given it is used verbatim —
    # the deterministic single-line cap (fit as many whole words as possible
    # within N characters, push the overflowing word to the next cue). Otherwise
    # it is DERIVED from the font so the cue fits one line (times `max_lines`).
    if max_chars is None:
        max_chars = _max_chars_for(canvas_width, font_px, safe_ratio) * max_lines

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in cleaned:
        if current and _chunk_chars(current + [word]) > max_chars:
            chunks.append(current)
            current = [word]
        else:
            current.append(word)
        text = (word.get("word") or "").strip()
        # Break on a hard word cap, or at a sentence/clause boundary once the cue
        # already carries `min_words` (so short sentences merge up toward the
        # target length instead of producing tiny one-word flashes, and long
        # sentences split on commas/clauses rather than mid-phrase at the cap).
        ends_clause = text.endswith(_SENTENCE_END) or text.endswith(_CLAUSE_END)
        if len(current) >= max_words or (
            ends_clause and len(current) >= min_words
        ):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    cues: list[dict[str, Any]] = []
    for chunk in chunks:
        if not chunk:
            continue
        cue: dict[str, Any] = {
            "text": " ".join(w["word"] for w in chunk),
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
        }
        cues.append(cue)
    return cues
