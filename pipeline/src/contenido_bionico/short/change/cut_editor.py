"""Launch the scoped CUT EDITOR agent.

A cut change (swap a take, drop a false start, tighten a silence, loosen a
too-aggressive cut) is applied by editing the run's `_intermediates/edl_render.json`
-- the ordered list of kept source ranges -- NOT by re-running the editorial
editor/reviewer/mapper loop. This module launches ONE bounded, tool-enabled
Claude-Code sub-session whose entire job is that single JSON edit; it edits the
file on disk and stops. `pipeline.surgical_recut` then rebuilds source/transcript
from the edited ranges and re-derives only the touched parts.

Result contract: an edit that LANDED is confirmed by the caller re-reading
`edl_render.json` and diffing the ranges (not by trusting the agent's words).
The editor's final `RESULT: {...}` line is parsed ONLY to recognize an explicit
DECLINE (`edited: false` + a Spanish `unsupported` reason -- e.g. the request
asks to ADD words never said, which the deletion-only contract forbids); that
raises `CutEditorDeclined` so `pipeline.surgical_recut` can propagate the reason
up to the dashboard as a rejected change instead of a red failure. An unchanged
file WITHOUT a decline reason stays a hard failure (ambiguous, caller-side).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from contenido_bionico.shared.runtime.agent_runner import (
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)

_HERE = Path(__file__).resolve()
PROMPT_PATH = _HERE.parent / "agents" / "cut_editor.md"
PIPELINE_DIR = _HERE.parents[4]            # repo/pipeline

MODEL = "claude-opus-4-8"
EFFORT = "high"
# One focused JSON edit: find the takes in word_for_word.json and rewrite the
# ranges. Bounded tight -- this is not a render loop.
AGENT_TIMEOUT_SECONDS = 10 * 60
MAX_TURNS = 25
# UTF-8 overlay merged OVER the runner's SANITIZED child env (never pass
# os.environ: the runner strips CLAUDE_CODE_*/CLAUDECODE harness vars so the
# sub-session doesn't behave as a nested claude and no-op). Also raise the
# agent's Bash per-command timeout (default 2 min) to the session budget so a
# verification command can't be killed mid-run.
AGENT_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "BASH_DEFAULT_TIMEOUT_MS": str(AGENT_TIMEOUT_SECONDS * 1000),
    "BASH_MAX_TIMEOUT_MS": str(AGENT_TIMEOUT_SECONDS * 1000),
}
AGENT_TOOLS = ["Read", "Edit", "Write", "Bash", "Grep"]


class CutEditorError(RuntimeError):
    pass


class CutEditorDeclined(CutEditorError):
    """The editor explicitly declined: the request cannot be honored as a
    deletion/reselection-only cut edit (e.g. it asks to ADD spoken content).
    Carries the editor's short Spanish `reason` so the change orchestrator can
    surface it as `unsupported` and the engine can post status='rejected'."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_result_line(text: str) -> dict | None:
    """Best-effort parse of the editor's final `RESULT: {...}` line.

    Scans every line for a `RESULT:` prefix (tolerating code fences) and keeps
    the LAST one that parses as a JSON object. Returns None when no usable
    RESULT line exists -- absence is NOT an error here (the caller verifies the
    edit by diffing the ranges); the line only matters to recognize a decline.
    """
    parsed: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`").strip()
        idx = line.find("RESULT:")
        if idx == -1:
            continue
        m = _JSON_OBJ_RE.search(line[idx + len("RESULT:"):])
        if not m:
            continue
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict):
            parsed = obj
    return parsed


def _build_message(run_dir: Path, notes: str) -> str:
    inter = run_dir / "_intermediates"
    edl = inter / "edl_render.json"
    wfw = inter / "word_for_word.json"
    edl_text = edl.read_text(encoding="utf-8-sig") if edl.exists() else "{}"
    return "\n\n".join([
        f"<RUN_DIR>\n{run_dir}\n</RUN_DIR>",
        f"<EDL_PATH>\n{edl}\n</EDL_PATH>",
        f"<WORD_FOR_WORD_PATH>\n{wfw}\n</WORD_FOR_WORD_PATH>",
        f"<USER_CHANGE_REQUEST>\n{notes.strip()}\n</USER_CHANGE_REQUEST>",
        "<CURRENT_EDL_RENDER>\n" + edl_text + "\n</CURRENT_EDL_RENDER>",
    ])


def run_cut_editor(video_id: str, notes: str, run_dir: Path) -> None:
    """Run the scoped cut editor; it edits `<run_dir>/_intermediates/edl_render.json`
    in place. Raises CutEditorError on missing prereqs, a dead agent CLI, timeout,
    or a non-zero agent exit; raises CutEditorDeclined when the editor's RESULT
    line explicitly declines with a Spanish reason (`edited: false` + a non-empty
    `unsupported`). Does NOT verify the edit landed -- the caller diffs the ranges
    (an unchanged file without a decline stays the caller's hard failure)."""
    if not (notes or "").strip():
        raise CutEditorError("el cambio de corte no puede estar vacio")
    if not PROMPT_PATH.exists():
        raise CutEditorError(f"falta el system prompt del editor de corte: {PROMPT_PATH}")
    edl = Path(run_dir) / "_intermediates" / "edl_render.json"
    if not edl.exists():
        raise CutEditorError(f"no existe edl_render.json para editar en {edl}")
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise CutEditorError(missing_agent_cmd_message())

    log_dir = Path(run_dir) / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_dir / f"{stamp}_cut_editor.log"
    message = _build_message(Path(run_dir), notes)

    try:
        result = run_agent_code(
            agent_cmd=agent_cmd,
            system_prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            initial_message=message,
            cwd=PIPELINE_DIR,
            timeout_seconds=AGENT_TIMEOUT_SECONDS,
            max_turns=MAX_TURNS,
            tools=AGENT_TOOLS,
            model=MODEL,
            effort=EFFORT,
            permission_mode="bypassPermissions",
            log_path=log_path,
            env=AGENT_ENV,
        )
    except subprocess.TimeoutExpired as exc:
        raise CutEditorError(
            "el editor de corte excedio el limite de %d minutos y fue detenido"
            % (AGENT_TIMEOUT_SECONDS // 60)
        ) from exc
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip().splitlines()
        raise CutEditorError(
            "el editor de corte fallo (exit %s): %s"
            % (result.returncode, " | ".join(tail[-3:])[:400])
        )

    # Structured decline: `edited: false` WITH a Spanish reason means the
    # request is outside the deletion-only contract (e.g. add new spoken
    # content). Propagate the reason so the change ends as 'rejected' on the
    # dashboard. `edited: false` without a reason (or no RESULT line at all)
    # falls through: the caller diffs the ranges and hard-fails the ambiguous
    # no-edit-no-decline case.
    parsed = _parse_result_line(result.stdout)
    if parsed is not None and parsed.get("edited") is False:
        reason = str(parsed.get("unsupported") or "").strip()
        if reason and reason.lower() not in {"null", "none"}:
            raise CutEditorDeclined(reason)
