"""Shared transport for the format-adaptation agents (V6.1).

Every adaptation agent — per-carousel content fitting, the chat conversation,
image phrases, infographic content — runs through `call_json_agent`: opus,
medium effort, tools-less, one JSON object on stdout, tolerant parse. The
producers own their prompts, validation, and fallbacks; this module owns only
the transport, so all agents share one battle-tested call/parse path (the one
`texts.py` shipped with).

Doctrine every adaptation prompt must carry: the transcript is the source.
Paraphrasing, abstraction, and format-specific setup/conclusion wording are
allowed — inventing facts, numbers, or claims the transcript does not support
is not.
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

AGENT_MODEL = "claude-opus-4-8"
AGENT_EFFORT = "medium"
AGENT_TIMEOUT_S = 300
AGENT_MAX_TURNS = 12

# Voice doctrine appended to EVERY text-agent system prompt — this module's
# `call_json_agent` plus the direct callers in texts.py / carousels.py (mitos) /
# hero_phrases.py. The output is always the person from the video speaking in
# FIRST PERSON, never a third-person narration about the piece ("el video
# afirma que…"). Kept in one place so every agent — and any future one —
# inherits it. Import and append it wherever a text prompt is loaded outside
# `call_json_agent`.
FIRST_PERSON_VOICE = (
    "\n\n## Voice — speak AS the person, first person (overrides everything above)\n"
    "Everything you output is said by the person from the video, in THEIR OWN "
    "voice (first person), as if saying it to camera or to a friend. NEVER "
    "narrate about the piece or describe the speaker from outside — no "
    '"el video…", "en este video", "el creador…", "the video shows / says / '
    'explains". Drop the meta-frame and state the idea directly, in the '
    "transcript's language. "
    'E.g. NOT "El video afirma que nadie sabe qué funciona" -> YES "Nadie sabe '
    'qué funciona".'
)


class AdaptError(RuntimeError):
    pass


def tagged(name: str, value: str) -> str:
    return f"<{name}>\n{value}\n</{name}>"


def clean_line(value: Any) -> str:
    """Collapse all whitespace in a string value to single spaces."""
    return re.sub(r"\s+", " ", str(value or "").strip())


def transcript_text(transcript: dict[str, Any] | None) -> str:
    """The corrected spoken script: every `word` token in order.

    Mirrors the texts producers; falls back to the transcript's top-level
    `text` field if the word list is empty for any reason. Empty string when
    there is no usable transcript.
    """
    if not isinstance(transcript, dict):
        return ""
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


def parse_json_object(stdout: str, who: str) -> dict[str, Any]:
    """Extract the one JSON object an agent returned.

    Tolerant of stray prose and code fences (start at the first `{`) AND of
    trailing junk after the object — a stray extra closing brace, a closing
    ``` fence, a sign-off line — via `raw_decode`, which parses the first
    complete JSON value and ignores everything after it.
    """
    start = stdout.find("{")
    if start == -1:
        raise AdaptError(f"{who} returned no JSON object: {stdout[:200]!r}")
    try:
        data, _ = json.JSONDecoder().raw_decode(stdout[start:])
    except json.JSONDecodeError as exc:
        raise AdaptError(f"{who} JSON invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise AdaptError(f"{who} JSON is not an object")
    return data


def call_json_agent(
    ctx,
    *,
    prompt_path: Path,
    log_name: str,
    message: str,
    model: str = AGENT_MODEL,
    effort: str = AGENT_EFFORT,
    timeout_s: int = AGENT_TIMEOUT_S,
    max_turns: int = AGENT_MAX_TURNS,
) -> dict[str, Any]:
    """One tools-less agent call -> the JSON object it printed.

    Raises `AdaptError` on every failure mode (no agent CLI, missing prompt,
    non-zero exit, unparseable output) so producers can fall back cleanly.
    Logs to `runs/<id>/logs/agent-calls/<log_name>.log` like every other
    formats agent.
    """
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise AdaptError(missing_agent_cmd_message())
    if not prompt_path.exists():
        raise AdaptError(f"adaptation prompt missing: {prompt_path}")
    config.load_env_into_process()
    log_dir = ctx.run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=prompt_path.read_text(encoding="utf-8") + FIRST_PERSON_VOICE,
        initial_message=message,
        cwd=config.REPO_ROOT,
        timeout_seconds=timeout_s,
        max_turns=max_turns,
        tools=[],
        model=model,
        effort=effort,
        permission_mode=None,
        log_path=log_dir / f"{log_name}.log",
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-400:]
        raise AdaptError(f"{log_name} agent failed (exit {result.returncode}); {tail}")
    return parse_json_object(result.stdout, log_name)
