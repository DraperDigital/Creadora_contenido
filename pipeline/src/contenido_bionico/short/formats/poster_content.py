"""One cached, neutral content distillation shared by every poster layout.

`content(ctx)` runs the `poster_content` agent ONCE per run (in-process memo +
`_intermediates/poster_content.json`, fingerprinted by the transcript) and
returns generic building blocks — headline, keywords, points, quote, summary —
that the layouts arrange. There is deliberately NO per-layout / per-genre agent:
the author never knows or assumes what a poster is "for", which is what keeps a
poster's text tied to the video instead of to an invented purpose.

Every field is capped here (the caps ARE the layout constraints) and degrades to
a bounded mechanical extraction from the carousel/quote plans when the agent is
unavailable, so posters still render.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

from contenido_bionico.short.formats import adapt

PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "poster_content.md"
CACHE_NAME = "poster_content.json"
CACHE_VERSION = 2  # v2: headline is ONE flat hero phrase (layout balancer breaks it)

# The hero phrase is stored as a single flat line — the poster layout's
# `balanceHeadline` breaks it into width-filling lines. We no longer pre-split
# into tiny 1-2 word lines (that stranded short words like "EL" and fragmented
# the phrase). ~42 chars fits a punchy 3-6 word statement.
HEADLINE_CHARS = 42
KEYWORD_CHARS = 12
POINT_HEAD_CHARS = 24
POINT_BODY_CHARS = 90
QUOTE_CHARS = 120
SUMMARY_CHARS = 240
MAX_POINTS = 5

_MEMO: dict[str, dict[str, Any] | None] = {}
_LOCK = threading.Lock()


def _reset_memo() -> None:
    with _LOCK:
        _MEMO.clear()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _cap(value: Any, chars: int) -> str:
    text = _clean(value)
    if len(text) <= chars:
        return text
    cut = text[: chars + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut[:chars].strip()


def _transcript(ctx) -> str:
    return adapt.transcript_text(ctx.transcript())


# --- normalize ----------------------------------------------------------------

def _norm_headline(raw: Any) -> list[str]:
    """The hero phrase as ONE flat line (single-element list). Never pre-split —
    the layout's balancer breaks it into width-filling lines, so pre-splitting
    here would only strand short words."""
    phrase = " ".join(_clean(x) for x in raw) if isinstance(raw, list) else _clean(raw)
    phrase = _cap(phrase, HEADLINE_CHARS)
    return [phrase] if phrase else []


def _norm_points(raw: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, dict):
                h = _cap(p.get("heading"), POINT_HEAD_CHARS)
                b = _cap(p.get("body"), POINT_BODY_CHARS)
            else:
                h, b = _cap(p, POINT_HEAD_CHARS), ""
            if h:
                out.append({"heading": h, "body": b})
            if len(out) >= MAX_POINTS:
                break
    return out


def _normalize(data: dict, fingerprint: str) -> dict:
    keywords = [_cap(k, KEYWORD_CHARS) for k in (data.get("keywords") or []) if _cap(k, KEYWORD_CHARS)][:3]
    return {
        "version": CACHE_VERSION,
        "fuente": fingerprint,
        "headline": _norm_headline(data.get("headline")),
        "keywords": keywords,
        "points": _norm_points(data.get("points")),
        "quote": _cap(data.get("quote"), QUOTE_CHARS),
        "summary": _cap(data.get("summary"), SUMMARY_CHARS),
    }


# --- fallback (mechanical, bounded) -------------------------------------------

def _plan_points(ctx) -> list[dict]:
    plan = ctx.carousel_plan()
    out: list[dict] = []
    if isinstance(plan, dict):
        for s in plan.get("slides") or []:
            if isinstance(s, dict) and s.get("kind") == "item":
                h = _cap(s.get("heading"), POINT_HEAD_CHARS)
                if h:
                    out.append({"heading": h, "body": _cap(s.get("body"), POINT_BODY_CHARS)})
    return out[:MAX_POINTS]


def _plan_title(ctx) -> str:
    plan = ctx.carousel_plan()
    if isinstance(plan, dict):
        for s in plan.get("slides") or []:
            if isinstance(s, dict) and s.get("kind") == "hero":
                return _clean(s.get("title"))
        return _clean(plan.get("title"))
    return ""


def _quotes(ctx) -> list[str]:
    plan = ctx.quote_plan()
    if not isinstance(plan, dict):
        return []
    return [_clean(q.get("text") if isinstance(q, dict) else q)
            for q in (plan.get("quotes") or [])
            if _clean(q.get("text") if isinstance(q, dict) else q)]


def _fallback(ctx, fingerprint: str) -> dict | None:
    title = _plan_title(ctx)
    points = _plan_points(ctx)
    quotes = _quotes(ctx)
    source = title or (quotes[0] if quotes else "")
    if not source and not points:
        return None
    headline = _norm_headline(" ".join(_clean(source).split()[:6]))
    keywords = [_cap(p["heading"].split()[0], KEYWORD_CHARS).upper() for p in points[:3] if p["heading"]]
    return {
        "version": CACHE_VERSION,
        "fuente": fingerprint,
        "headline": headline,
        "keywords": keywords,
        "points": points,
        "quote": _cap(quotes[0], QUOTE_CHARS) if quotes else "",
        "summary": _cap(title, SUMMARY_CHARS),
    }


# --- cache + agent ------------------------------------------------------------

def _fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _cache_path(ctx) -> Path:
    return ctx.run_dir / "_intermediates" / CACHE_NAME


def _read_cache(ctx, fingerprint: str) -> dict | None:
    try:
        data = json.loads(_cache_path(ctx).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or data.get("fuente") != fingerprint:
        return None
    return data


def _write_cache(ctx, payload: dict) -> None:
    path = _cache_path(ctx)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _call_agent(ctx, text: str) -> dict:
    parts = ["Run the poster_content job.",
             "Return only the JSON object on stdout. Do not use tools or read files.",
             adapt.tagged("TRANSCRIPT", text)]
    title = _plan_title(ctx)
    if title:
        parts.append(adapt.tagged("TITULO", title))
    points = _plan_points(ctx)
    if points:
        parts.append(adapt.tagged("PUNTOS", "\n".join(f"- {p['heading']}: {p['body']}" for p in points)))
    return adapt.call_json_agent(ctx, prompt_path=PROMPT_PATH, log_name="poster_content", message="\n".join(parts))


def content(ctx) -> dict | None:
    """The run's neutral poster content, or None when nothing can be built."""
    key = str(ctx.run_dir)
    with _LOCK:
        if key in _MEMO:
            return _MEMO[key]
    text = _transcript(ctx)
    fingerprint = _fingerprint(text)
    cached = _read_cache(ctx, fingerprint) if text else None
    if cached is not None:
        with _LOCK:
            _MEMO[key] = cached
        return cached
    payload: dict | None
    try:
        if not text:
            raise adapt.AdaptError("transcript has no spoken text")
        payload = _normalize(_call_agent(ctx, text), fingerprint)
        if not payload["headline"]:
            raise adapt.AdaptError("content agent returned no headline")
        _write_cache(ctx, payload)
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[formats] poster_content no disponible, uso extraccion mecanica ({exc})",
              file=sys.stderr, flush=True)
        payload = _fallback(ctx, fingerprint)
    with _LOCK:
        _MEMO[key] = payload
    return payload
