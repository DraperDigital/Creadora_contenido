"""One shared adaptation call for the quote/phrase image and video formats.

`adapted_citas` / `adapted_frase` / `hero_word` all come from a single cached
opus call (`agents/frases_adapt.md`) that refits the run's pulled quotes into
lines that work as standalone visuals, plus one poster phrase and one
magazine-cover mega-word. The call is cached per run (in-process memo +
`_intermediates/adapt_frases.json`, fingerprinted by the source quotes) so the
several consumers — cita_foto, citas_3en1, frase_*, carrusel_citas,
quote_foto — spend one agent call between them.

Every accessor degrades to the raw quote plan on any failure, so consumers
keep the pre-adaptation behavior when the agent is unavailable. The verbatim
karaoke format must NOT use these (it needs the raw quote text to match the
spoken words).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

from contenido_bionico.short.formats.adapt import (
    AdaptError,
    call_json_agent,
    clean_line,
    tagged,
    transcript_text,
)

PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "frases_adapt.md"
CACHE_NAME = "adapt_frases.json"
CACHE_VERSION = 2         # bumped when `idea` was added (v1 caches re-run cleanly)

MAX_CITA_CHARS = 200      # a fitted quote longer than this is truncated at a word
MAX_FRASE_CHARS = 120     # poster phrase ceiling
MAX_WORD_CHARS = 18       # mega-word hard ceiling (prompt asks for <= 16)
MAX_WORD_TOKENS = 3
MAX_IDEA_WORDS = 7        # "big idea" phrase for the text-behind-person format
MIN_IDEA_WORDS = 3        # below this the agent idea is treated as too thin

# In-process memo so concurrent producers (runner pool) trigger at most one
# agent call per run, even before the disk cache exists.
_MEMO: dict[str, dict[str, Any] | None] = {}
_LOCK = threading.Lock()

_WORD_RX = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]+")
_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "en",
    "que", "por", "para", "con", "sin", "los", "es", "son", "no", "si", "se",
    "tu", "su", "mi", "y", "o", "a", "al", "lo", "más", "mas", "como", "pero",
}


def _reset_memo() -> None:
    """Test hook: forget every memoized payload."""
    with _LOCK:
        _MEMO.clear()


def _raw_quotes(ctx) -> list[str]:
    plan = ctx.quote_plan()
    if not isinstance(plan, dict):
        return []
    out: list[str] = []
    for item in plan.get("quotes") or []:
        text = clean_line(item.get("text") if isinstance(item, dict) else item)
        if text:
            out.append(text)
    return out


def _fingerprint(quotes: list[str]) -> str:
    return hashlib.sha1("\n".join(quotes).encode("utf-8")).hexdigest()


def _truncate_words(text: str, limit: int) -> str:
    """Cut `text` to <= `limit` chars at a word boundary (no trailing ellipsis)."""
    text = clean_line(text)
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut[:limit].strip()


def _first_words(text: str, n: int) -> str:
    """The first `n` whitespace-delimited words of `text` (collapsed)."""
    return " ".join(clean_line(text).split()[:n]).strip()


def _normalize(data: dict[str, Any], raw_quotes: list[str]) -> dict[str, Any]:
    """Validate/clamp the agent payload into the canonical cached shape.

    `citas` is forced to the raw quotes' length and order (short agent lists
    are padded with the raw text) so index-paired consumers stay aligned.
    """
    citas_in = data.get("citas")
    citas: list[str] = []
    for i, raw in enumerate(raw_quotes):
        fitted = ""
        if isinstance(citas_in, list) and i < len(citas_in):
            fitted = _truncate_words(clean_line(citas_in[i]), MAX_CITA_CHARS)
        citas.append(fitted or raw)

    frase = _truncate_words(clean_line(data.get("frase")), MAX_FRASE_CHARS)

    palabra = clean_line(data.get("palabra"))
    tokens = palabra.split()
    if len(tokens) > MAX_WORD_TOKENS:
        palabra = " ".join(tokens[:MAX_WORD_TOKENS])
    if len(palabra) > MAX_WORD_CHARS:
        palabra = palabra.split()[0][:MAX_WORD_CHARS] if palabra.split() else ""

    # `idea`: the 3-7 word main idea of the whole video (text-behind-person).
    # Too-thin agent ideas (< MIN_IDEA_WORDS) are dropped so the accessor
    # falls back to a fuller phrase; longer ones are clamped to MAX_IDEA_WORDS.
    idea = _first_words(data.get("idea"), MAX_IDEA_WORDS)
    if len(idea.split()) < MIN_IDEA_WORDS:
        idea = ""

    return {
        "version": CACHE_VERSION,
        "fuente": _fingerprint(raw_quotes),
        "citas": citas,
        "frase": frase,
        "palabra": palabra,
        "idea": idea,
    }


def _cache_path(ctx) -> Path:
    return ctx.run_dir / "_intermediates" / CACHE_NAME


def _read_cache(ctx, fingerprint: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path(ctx).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    if data.get("fuente") != fingerprint or not isinstance(data.get("citas"), list):
        return None
    return data


def _write_cache(ctx, payload: dict[str, Any]) -> None:
    path = _cache_path(ctx)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # cache is an optimization; never fail the payload over it


def _call_agent(ctx, raw_quotes: list[str]) -> dict[str, Any]:
    """The single adaptation call. Isolated so tests monkeypatch one seam."""
    text = transcript_text(ctx.transcript())
    if not text:
        raise AdaptError("transcript has no spoken text")
    citas_lines = "\n".join(f"{i}. {q}" for i, q in enumerate(raw_quotes, start=1))
    message = "\n".join(
        [
            "Run the Frases job.",
            "Return only the JSON object on stdout. Do not use tools or read files.",
            tagged("TRANSCRIPT", text),
            tagged("CITAS", citas_lines),
        ]
    )
    return call_json_agent(
        ctx, prompt_path=PROMPT_PATH, log_name="frases_adapt", message=message
    )


def _payload(ctx) -> dict[str, Any] | None:
    """The normalized adaptation payload for this run, or None on failure.

    Memoized per run_dir; persisted to `_intermediates/adapt_frases.json`
    (keyed to the raw quotes' fingerprint, so a recut/replan re-runs the
    agent). Failures memoize as None for this process only — a later stage
    invocation may retry.
    """
    key = str(ctx.run_dir)
    with _LOCK:
        if key in _MEMO:
            return _MEMO[key]
        raw_quotes = _raw_quotes(ctx)
        if not raw_quotes:
            _MEMO[key] = None
            return None
        fingerprint = _fingerprint(raw_quotes)
        cached = _read_cache(ctx, fingerprint)
        if cached is not None:
            _MEMO[key] = cached
            return cached
        try:
            payload = _normalize(_call_agent(ctx, raw_quotes), raw_quotes)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            print(f"[formats] frases_adapt no disponible, uso citas originales ({exc})",
                  file=sys.stderr, flush=True)
            _MEMO[key] = None
            return None
        _write_cache(ctx, payload)
        _MEMO[key] = payload
        return payload


def _fallback_word(text: str) -> str | None:
    """Deterministic magazine word from a quote: its longest significant token."""
    tokens = [t for t in _WORD_RX.findall(text or "") if t.lower() not in _STOPWORDS]
    if not tokens:
        tokens = _WORD_RX.findall(text or "")
    if not tokens:
        return None
    return max(tokens, key=len)[:MAX_WORD_CHARS].upper()


# --- public accessors (always safe; fall back to the raw quote plan) ----------

def adapted_citas(ctx) -> list[str]:
    """Format-fitted quote texts, same count/order as the raw quote plan.

    Falls back to the raw quotes when the agent is unavailable; `[]` when the
    run has no quotes at all (consumers skip exactly as before).
    """
    payload = _payload(ctx)
    if payload is not None:
        return list(payload["citas"])
    return _raw_quotes(ctx)


def adapted_frase(ctx) -> str | None:
    """One poster-worthy hero phrase, else the strongest raw quote, else None."""
    payload = _payload(ctx)
    if payload is not None and payload.get("frase"):
        return payload["frase"]
    raw = _raw_quotes(ctx)
    return raw[0] if raw else None


def hero_word(ctx) -> str | None:
    """The 1-3 word magazine-cover text for `frase_detras`.

    Agent-chosen when available, else a deterministic significant word from
    the strongest quote, else None (the producer skips).
    """
    payload = _payload(ctx)
    if payload is not None and payload.get("palabra"):
        return payload["palabra"]
    raw = _raw_quotes(ctx)
    return _fallback_word(raw[0]) if raw else None


def main_idea(ctx) -> str | None:
    """The 3-7 word "big idea" phrase for the text-behind-person format.

    Agent-chosen when available; otherwise the first `MAX_IDEA_WORDS` words of
    the poster phrase (`adapted_frase`), else of the strongest raw quote. None
    only when the run has no quotes at all (the producer then skips).
    """
    payload = _payload(ctx)
    if payload is not None and payload.get("idea"):
        return payload["idea"]
    frase = adapted_frase(ctx)
    if frase:
        return _first_words(frase, MAX_IDEA_WORDS)
    return None
