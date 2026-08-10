"""Text-deliverable producers (category `textos`).

Two agent calls turn a finished short's transcript (plus the carousel plan and
the pulled quotes when available) into the 10 text deliverables:

  produce_social   -> caption.txt, primer_comentario.txt, youtube_short.txt,
                      post_x.txt, ganchos.txt
  produce_longform -> hilo_x.txt, articulo_x.txt, linkedin.txt, newsletter.txt,
                      blog.txt

Both mirror the carousel orchestrator's agent-call pattern exactly (opus,
medium effort, tools-less, 300 s timeout, 12 turns, tolerant JSON parse). The
grounding inputs come from the `FormatContext` atoms; nothing is invented here.

Robustness: an agent failure or unparseable JSON raises `TextsError` — the
runner catches it, logs it to `logs/formats/<key>.log`, and continues. If the
agent returns only SOME of the expected keys, we write every file we can build
from the keys that ARE present and skip the rest (a partial set of real
deliverables beats failing the whole producer). A producer that can build
nothing returns `[]` (a clean skip).

Plain text contract: every deliverable is pasted into a platform that renders
markdown as literal characters (X, Instagram, LinkedIn, YouTube, email). The
prompts forbid markdown outright, and `_strip_markdown` scrubs any slips from
every string before it is written (the hashtags line is the one value passed
through untouched).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from contenido_bionico.shared import config
from contenido_bionico.shared.runtime.agent_runner import (
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)
from contenido_bionico.short.formats import adapt

_AGENTS_DIR = Path(__file__).resolve().parent / "agents"
_SOCIAL_PROMPT = _AGENTS_DIR / "texts_social.md"
_LONGFORM_PROMPT = _AGENTS_DIR / "texts_longform.md"

AGENT_MODEL = "claude-opus-4-8"
AGENT_EFFORT = "medium"
AGENT_TIMEOUT_S = 300
AGENT_MAX_TURNS = 12


class TextsError(RuntimeError):
    pass


# --- inputs (grounding) ---------------------------------------------------

def _tagged(name: str, value: str) -> str:
    return f"<{name}>\n{value}\n</{name}>"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _transcript_text(transcript: dict[str, Any]) -> str:
    """The corrected spoken script: every `word` token in order.

    Mirrors the carousel orchestrator; falls back to the transcript's top-level
    `text` field if the word list is empty for any reason.
    """
    words = transcript.get("words") or []
    parts = [
        str(w.get("text") or w.get("word") or "").strip()
        for w in words
        if isinstance(w, dict) and w.get("type") == "word"
    ]
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if text:
        return text
    return re.sub(r"\s+", " ", str(transcript.get("text") or "")).strip()


def _carousel_inputs(ctx) -> tuple[str | None, str | None]:
    """`(<TITULO>, <PUNTOS>)` text from the carousel plan, or `(None, None)`."""
    plan = ctx.carousel_plan()
    if not isinstance(plan, dict):
        return None, None
    title = _clean(plan.get("title")) or None
    points: list[str] = []
    for slide in plan.get("slides") or []:
        if not isinstance(slide, dict) or slide.get("kind") != "item":
            continue
        heading = _clean(slide.get("heading"))
        body = _clean(slide.get("body"))
        if heading and body:
            points.append(f"- {heading}: {body}")
        elif heading:
            points.append(f"- {heading}")
        elif body:
            points.append(f"- {body}")
    return title, ("\n".join(points) or None)


def _quote_inputs(ctx) -> str | None:
    """`<CITAS>` text (the quote texts, one per line) or `None`."""
    plan = ctx.quote_plan()
    if not isinstance(plan, dict):
        return None
    lines: list[str] = []
    for quote in plan.get("quotes") or []:
        if isinstance(quote, dict):
            text = _clean(quote.get("text"))
            if text:
                lines.append(f"- {text}")
    return "\n".join(lines) or None


def _build_message(ctx, *, job_line: str) -> str:
    transcript = ctx.transcript()
    if not isinstance(transcript, dict):
        raise TextsError("transcript.json missing or unreadable")
    text = _transcript_text(transcript)
    if not text:
        raise TextsError("transcript has no spoken text")
    parts = [
        job_line,
        "Return only the JSON object on stdout. Do not use tools or read files.",
        _tagged("TRANSCRIPT", text),
    ]
    title, points = _carousel_inputs(ctx)
    if title:
        parts.append(_tagged("TITULO", title))
    if points:
        parts.append(_tagged("PUNTOS", points))
    citas = _quote_inputs(ctx)
    if citas:
        parts.append(_tagged("CITAS", citas))
    return "\n".join(parts)


# --- agent call + parse (mirrors carousel/orchestrator) -------------------

def _parse_json(stdout: str, who: str) -> dict[str, Any]:
    """Extract the one JSON object the agent returned.

    Tolerant of stray prose and code fences (start at the first `{`) AND of
    trailing junk after the object — a stray extra closing brace, a closing
    ``` fence, a sign-off line — via `raw_decode`, which parses the first
    complete JSON value and ignores everything after it. (A plain
    first-`{`/last-`}` slice breaks when opus appends an extra `}`, which it
    does on long outputs.)
    """
    start = stdout.find("{")
    if start == -1:
        raise TextsError(f"{who} returned no JSON object: {stdout[:200]!r}")
    try:
        data, _ = json.JSONDecoder().raw_decode(stdout[start:])
    except json.JSONDecodeError as exc:
        raise TextsError(f"{who} JSON invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise TextsError(f"{who} JSON is not an object")
    return data


def _call_agent(ctx, prompt_path: Path, log_name: str, message: str) -> dict[str, Any]:
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise TextsError(missing_agent_cmd_message())
    if not prompt_path.exists():
        raise TextsError(f"texts system prompt missing: {prompt_path}")
    config.load_env_into_process()
    log_dir = ctx.run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=prompt_path.read_text(encoding="utf-8") + adapt.FIRST_PERSON_VOICE,
        initial_message=message,
        cwd=config.REPO_ROOT,
        timeout_seconds=AGENT_TIMEOUT_S,
        max_turns=AGENT_MAX_TURNS,
        tools=[],
        model=AGENT_MODEL,
        effort=AGENT_EFFORT,
        permission_mode=None,
        log_path=log_dir / f"{log_name}.log",
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-400:]
        raise TextsError(f"{log_name} agent failed (exit {result.returncode}); {tail}")
    return _parse_json(result.stdout, log_name)


# --- plain-text defense ---------------------------------------------------
#
# The platforms these files are pasted into render markdown as literal
# characters, so the prompts forbid it. This layer conservatively removes the
# common slips anyway. Conservative means: legitimate prose is never mangled —
# a lone asterisk, snake_case, math like `2*3` or `x**2`, `#Hashtags` (no
# space after `#`) and `> 100` comparisons are all left alone.

_MD_FENCE_LINE = re.compile(r"^\s*```[\w+-]*\s*$")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s")
_MD_LINK = re.compile(r"!?\[([^\[\]\n]+)\]\(([^()\n]*)\)")
_MD_BOLD_AST = re.compile(r"(?<![\w*])\*\*(?![\s*])([^*\n]{2,}?)(?<![\s*])\*\*(?![\w*])")
_MD_BOLD_UND = re.compile(r"(?<!\w)__(?![\s_])([^_\n]{2,}?)(?<![\s_])__(?!\w)")
_MD_ITALIC_AST = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]{2,}?)(?<![\s*])\*(?![\w*])")
_MD_ITALIC_UND = re.compile(r"(?<!\w)_(?![\s_])([^_\n]{2,}?)(?<![\s_])_(?!\w)")
_MD_CODE = re.compile(r"`([^`\n]+)`")


def _link_text(match: re.Match) -> str:
    """`[text](url)` -> `text (url)`, or just `text` when the url is empty."""
    text = match.group(1)
    url = match.group(2).strip()
    return f"{text} ({url})" if url else text


def _strip_markdown(text: str) -> str:
    """Conservatively strip markdown syntax so the text pastes as plain text.

    Removes: leading `#{1,6} ` heading markers, ``` fence lines, leading `> `
    blockquote markers, `**x**`/`__x__` bold and `*x*`/`_x_` italics (only
    when wrapping >= 2 characters, at emphasis-like boundaries), `` `x` ``
    inline code, and `[text](url)` links (kept as `text (url)`).
    Markdown-free text passes through byte-identical.
    """
    lines: list[str] = []
    for line in text.split("\n"):
        if _MD_FENCE_LINE.match(line):
            continue
        while True:  # peel `> ` (and nested `> > `) then any heading marker
            unquoted = _MD_BLOCKQUOTE.sub("", line)
            if unquoted == line:
                break
            line = unquoted
        line = _MD_HEADING.sub("", line)
        lines.append(line)
    out = "\n".join(lines)
    out = _MD_LINK.sub(_link_text, out)
    out = _MD_BOLD_AST.sub(r"\1", out)
    out = _MD_BOLD_UND.sub(r"\1", out)
    out = _MD_ITALIC_AST.sub(r"\1", out)
    out = _MD_ITALIC_UND.sub(r"\1", out)
    out = _MD_CODE.sub(r"\1", out)
    return out


# --- writing helpers ------------------------------------------------------

def _write(out_dir: Path, name: str, content: str) -> Path:
    path = out_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def _str(data: dict[str, Any], key: str) -> str | None:
    """A non-empty string value for `key`, else None (partial-key tolerance).

    Values are passed through `_strip_markdown`: deliverables are plain text.
    """
    value = data.get(key)
    if not isinstance(value, str):
        return None
    value = _strip_markdown(value)
    return value if value.strip() else None


def _str_list(data: dict[str, Any], key: str, *, strip_md: bool = True) -> list[str]:
    """Non-empty, stripped string items of a list value (else `[]`).

    Items are passed through `_strip_markdown` unless `strip_md=False`
    (hashtags are the one list written verbatim).
    """
    value = data.get(key)
    if not isinstance(value, list):
        return []
    items = [item for item in value if isinstance(item, str)]
    if strip_md:
        items = [_strip_markdown(item) for item in items]
    return [item.strip() for item in items if item.strip()]


# --- producers ------------------------------------------------------------

def produce_social(ctx) -> list[Path]:
    """caption / primer_comentario / youtube_short / post_x / ganchos."""
    message = _build_message(ctx, job_line="Run the Social Texts job.")
    data = _call_agent(ctx, _SOCIAL_PROMPT, "texts_social", message)

    out_dir = ctx.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    caption = _str(data, "caption")
    if caption is not None:
        hashtags = _str_list(data, "hashtags", strip_md=False)
        content = caption + "\n\n" + " ".join(hashtags) if hashtags else caption
        written.append(_write(out_dir, "caption.txt", content))

    comentario = _str(data, "primer_comentario")
    if comentario is not None:
        written.append(_write(out_dir, "primer_comentario.txt", comentario))

    yt_title = _str(data, "youtube_title")
    yt_desc = _str(data, "youtube_description")
    if yt_title is not None and yt_desc is not None:
        written.append(
            _write(
                out_dir,
                "youtube_short.txt",
                f"TÍTULO:\n{yt_title.strip()}\n\nDESCRIPCIÓN:\n{yt_desc.strip()}\n",
            )
        )

    post_x = _str(data, "post_x")
    if post_x is not None:
        written.append(_write(out_dir, "post_x.txt", post_x))

    ganchos = _str_list(data, "ganchos")
    if ganchos:
        content = "".join(f"{i}. {g}\n" for i, g in enumerate(ganchos, start=1))
        written.append(_write(out_dir, "ganchos.txt", content))

    return written


def produce_longform(ctx) -> list[Path]:
    """hilo_x / articulo_x / linkedin / newsletter / blog."""
    message = _build_message(ctx, job_line="Run the Longform Texts job.")
    data = _call_agent(ctx, _LONGFORM_PROMPT, "texts_longform", message)

    out_dir = ctx.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    tweets = _str_list(data, "hilo_x")
    if tweets:
        written.append(_write(out_dir, "hilo_x.txt", "\n\n---\n\n".join(tweets)))

    articulo = _str(data, "articulo_x")
    if articulo is not None:
        written.append(_write(out_dir, "articulo_x.txt", articulo))

    linkedin = _str(data, "linkedin")
    if linkedin is not None:
        written.append(_write(out_dir, "linkedin.txt", linkedin))

    newsletter = data.get("newsletter")
    if isinstance(newsletter, dict):
        asuntos = _str_list(newsletter, "asuntos")
        cuerpo = _str(newsletter, "cuerpo")
        if asuntos and cuerpo is not None:
            content = "ASUNTOS:\n- " + "\n- ".join(asuntos) + f"\n\nCUERPO:\n{cuerpo}\n"
            written.append(_write(out_dir, "newsletter.txt", content))

    blog = _str(data, "blog")
    if blog is not None:
        written.append(_write(out_dir, "blog.txt", blog))

    return written
