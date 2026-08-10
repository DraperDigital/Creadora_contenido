"""Change orchestrator: an AUTONOMOUS editor agent that applies ANY change.

Flow: `run_change(video_id, target, notes, anim_opts)` builds a light context for
the (already forked) run — including a REGISTRY summary of the target's parts —
then launches a tool-enabled Claude-Code sub-session (the `change_orchestrator`
agent). The agent routes each request through the registry: it edits the part's
per-run intermediate (or respawns the producer with notes), then calls the two
generic primitives `rerender_part(video_id, part)` + `recompose(video_id)` — it
does NOT re-read pipeline source to learn how to render (the primitives do it),
and it writes only inside the run dir. The agent ends by printing a
`RESULT: {...}` line naming what it changed; we parse it, make sure the changed
deliverables are published, and return `{changed, summary, unsupported}`.

There is NO fixed tool menu: the operator rejected building one Python helper per
request (there are infinite possible change requests). The routing knowledge
lives in the registry (`short/change/registry.py`) + the two primitives
(`short/change/primitives.py`), taught to the agent by its system prompt
(`agents/change_orchestrator.md`); this module only builds the message (folding
in the registry map + a UTF-8 env), launches the agent under a bounded turn
budget, parses its result, and guarantees publish.

The `.md` file is ONLY the LLM system prompt.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from contenido_bionico.shared.runtime.agent_runner import (
    HeartbeatInfo,
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)
from contenido_bionico.short.change.registry import PARTS

_HERE = Path(__file__).resolve()
AGENTS_DIR = _HERE.parent / "agents"
PROMPT_PATH = AGENTS_DIR / "change_orchestrator.md"
PIPELINE_DIR = _HERE.parents[4]            # repo/pipeline
RUNS_DIR = PIPELINE_DIR / "runs"

MODEL = "claude-opus-4-8"
EFFORT = "high"
# Base wall-clock budget for the change session (surgical/cut/look edits).
# Env-tunable: BIONICO_CHANGE_TIMEOUT_MINUTES overrides the 60-minute default.
AGENT_TIMEOUT_SECONDS = 60 * 60
# Larger budget for edits that can legitimately CASCADE into scene
# re-authoring (a video target whose run has animated scenes: the scene
# respawn and surgical_recut both re-author scenes). Env-tunable:
# BIONICO_CHANGE_CASCADE_TIMEOUT_MINUTES overrides the 120-minute default.
CASCADE_TIMEOUT_SECONDS = 2 * 60 * 60
# One cheap, tool-less follow-up turn used ONLY to recover a missing RESULT
# line from the finished session's transcript tail (never to redo work).
FOLLOWUP_MODEL = "claude-sonnet-5"
FOLLOWUP_TIMEOUT_SECONDS = 5 * 60
# Bounded turn budget. The agent no longer pastes multi-step render recipes:
# a change is now (edit an intermediate | respawn a producer) -> `rerender_part`
# for the touched part(s) -> `recompose` ONCE -> verify. Heavy edits can spend
# most of an hour inside deterministic render helpers; MAX_TURNS bounds agent
# improvisation while this wall-clock cap keeps legitimate recut + reanimate
# work alive long enough to finish. The registry + two primitives still keep
# the agent on the cheap path: few turns instead of re-reading
# pipeline source — deliberately NOT the model/effort (kept high per the operator).
MAX_TURNS = 40
# Force UTF-8 in the agent's `python -c` child processes so a stray unicode
# char in a print (e.g. a `->` rendered as an arrow) can't crash a 25-min run
# on a cp1252 Windows console. Merged OVER the runner's sanitized child env
# (never the raw parent env: run_agent_code strips CLAUDE_CODE_*/CLAUDECODE/
# ANTHROPIC_* harness vars so the sub-session runs as a clean standalone
# `claude` — re-adding os.environ here would undo that isolation).
AGENT_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
# How often the change session prints a one-line liveness heartbeat to stdout.
# The engine's watchdog kills a job whose log stops growing for 30 minutes
# (BIONICO_STALL_MINUTES); the agent's Bash output is drained to sidecar logs
# only, so without this the job log stays silent for the whole 60-120 min
# session and every long change dies at the 30-minute wall.
HEARTBEAT_INTERVAL_SECONDS = 120.0
AGENT_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]

TARGETS = ("video", "carousel", "quotes")


class ChangeError(RuntimeError):
    pass


def _run_dir(video_id: str) -> Path:
    return RUNS_DIR / str(video_id)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _output_dir(video_id: str) -> Path | None:
    import contenido_bionico.pipeline as pl  # lazy: avoids import cycle
    return pl.run_output_dir(video_id)


def _env_seconds(name: str, default_seconds: int) -> int:
    """A positive integer MINUTES override from env `name`, in seconds;
    falls back to `default_seconds` when unset/invalid."""
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            minutes = int(raw)
            if minutes > 0:
                return minutes * 60
        except ValueError:
            pass
    return default_seconds


def _timeout_seconds_for(context: dict) -> int:
    """Per-change wall-clock budget.

    Surgical/cut/look edits keep the tight base budget (default 60 min,
    BIONICO_CHANGE_TIMEOUT_MINUTES). Only a VIDEO edit over a run that has
    animated scenes can legitimately cascade into scene re-authoring (the
    scene respawn / surgical_recut re-author scenes), so exactly those runs
    get the larger cascade budget (default 120 min,
    BIONICO_CHANGE_CASCADE_TIMEOUT_MINUTES).
    """
    base = _env_seconds("BIONICO_CHANGE_TIMEOUT_MINUTES", AGENT_TIMEOUT_SECONDS)
    if context.get("target") == "video" and (context.get("scene_count") or 0) > 0:
        cascade = _env_seconds(
            "BIONICO_CHANGE_CASCADE_TIMEOUT_MINUTES", CASCADE_TIMEOUT_SECONDS
        )
        return max(base, cascade)
    return base


# ---------- session transcript scanning (progress + declines) ----------

_PROGRESS_MARKERS = (
    "[progress]",
    "[surgical_recut]",
    "[edit] planner",
    "[rerender_part]",
    "[recompose]",
    "[short]",
    "[pipeline]",
)

_DECLINE_RE = re.compile(r"DECLINADO:\s*([^\r\n\"\\]+)")


def _session_transcript_text(log_path: Path | None, *extra: str | None) -> str:
    """Best-effort concatenation of the session's textual traces: any extra
    strings (final stdout, TimeoutExpired.output) plus the tails of the
    runner's sidecar logs (stream events carry the Bash tool output, which is
    where pipeline progress lines and DECLINADO markers actually live)."""
    parts: list[str] = [t for t in extra if t]
    if log_path is not None:
        for suffix in (".stream.jsonl", ".stderr.log"):
            try:
                sidecar = log_path.with_suffix(suffix)
                if sidecar.exists():
                    parts.append(
                        sidecar.read_text(encoding="utf-8", errors="replace")[-200_000:]
                    )
            except OSError:
                continue
    return "\n".join(parts)


def _partial_progress(text: str) -> str | None:
    """The LAST recognizable pipeline progress marker in the transcript, for
    human timeout messages ('ultimo progreso: ...')."""
    last: str | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in _PROGRESS_MARKERS):
            last = stripped
    return last[:200] if last else None


def _detect_decline(text: str) -> str | None:
    """The LAST `DECLINADO: <razon>` marker in the transcript (printed by
    `pipeline.surgical_recut` when the cut editor declines), or None."""
    last: str | None = None
    for match in _DECLINE_RE.finditer(text or ""):
        reason = match.group(1).strip().strip("'\" ")
        if reason:
            last = reason
    return last[:300] if last else None


# ---------- IRON LAW integrity: pipeline source must not change ----------

_SNAPSHOT_EXCLUDE_DIR_NAMES = {"__pycache__", "node_modules", ".git"}


def _snapshot_source_tree() -> dict[str, tuple[int, int]] | None:
    """(mtime_ns, size) per file under `pipeline/src`, excluding the areas a
    legitimate render writes to: the per-run staged component copies
    (`shared/remotion/src/runs/`), the staged public assets
    (`shared/remotion/public/`), and the transient per-render entrypoints
    (`index.<token>.tsx` / `Compositions.<token>.ts`). Never raises; returns
    None when the source root is missing (sandboxed tests)."""
    root = PIPELINE_DIR / "src"
    if not root.is_dir():
        return None
    remotion_dir = root / "contenido_bionico" / "shared" / "remotion"
    exclude_prefixes = tuple(
        str(p) for p in (remotion_dir / "src" / "runs", remotion_dir / "public")
    )
    remotion_src = str(remotion_dir / "src")
    snapshot: dict[str, tuple[int, int]] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _SNAPSHOT_EXCLUDE_DIR_NAMES
                and not str(Path(dirpath) / d).startswith(exclude_prefixes)
            ]
            for name in filenames:
                if name.endswith((".pyc", ".pyo")):
                    continue
                if dirpath == remotion_src and (
                    (name.startswith("index.") and name.endswith(".tsx"))
                    or name.startswith("Compositions.")
                ):
                    continue  # transient per-render entrypoint pair
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                snapshot[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    return snapshot


def _fail_if_source_tree_dirty(before: dict[str, tuple[int, int]] | None) -> None:
    """IRON LAW enforcement: after the agent session, any modified/deleted/new
    file under `pipeline/src` (outside the excluded render-staging areas) fails
    the change loudly — an edit must only ever write inside `runs/<id>/`."""
    if before is None:
        return
    after = _snapshot_source_tree()
    if after is None:
        return
    diffs = sorted(
        {p for p, sig in before.items() if after.get(p) != sig}
        | {p for p in after if p not in before}
    )
    if not diffs:
        return
    shown = ", ".join(diffs[:5])
    if len(diffs) > 5:
        shown += f", +{len(diffs) - 5} mas"
    raise ChangeError(
        "el agente de cambios modifico codigo fuente del pipeline fuera de "
        f"runs/ (prohibido): {shown}. El cambio se descarta; revisa el log "
        "del agente y restaura esos archivos."
    )


# ---------- light context snapshot the agent starts from ----------

def _current_music(rd: Path) -> dict:
    plan = _read_json(rd / "Audio_Plan.json")
    music = plan.get("music") if isinstance(plan, dict) else None
    if not isinstance(music, dict):
        return {"file": None, "base_gain_db": None}
    file = music.get("file")
    return {
        "file": Path(str(file)).name if file else None,
        "base_gain_db": music.get("base_gain_db"),
    }


def inspect_run(video_id: str, target: str) -> dict:
    """A light, best-effort snapshot the agent starts from. The files on disk in
    RUN_DIR are ground truth; this is only a hint so the agent knows the shape of
    the run without re-deriving everything. Never raises."""
    rd = _run_dir(video_id)
    out = _output_dir(video_id)
    ctx: dict[str, Any] = {
        "target": target,
        "video_id": str(video_id),
        "run_dir": str(rd),
        "output_dir": str(out) if out else None,
    }
    try:
        if target == "video":
            anim = rd / "animations"
            scenes = sorted(p.name for p in anim.glob("*") if p.is_dir()) if anim.exists() else []
            ctx["scene_ids"] = scenes
            ctx["scene_count"] = len(scenes)
            ctx["captions_on"] = (rd / "captions.webm").exists()
            ctx["caption_style"] = _read_json(rd / "Captions_Style.json") or None
            ctx["music"] = _current_music(rd)
        elif target == "carousel":
            ctx["slide_count"] = len(list(out.glob("carrusel_slide*.png"))) if out else 0
        elif target == "quotes":
            ctx["quote_count"] = len(list(out.glob("quote_*.mp4"))) if out else 0
    except OSError:
        pass
    return ctx


# ---------- result parsing ----------

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_result_line(text: str) -> dict:
    """Pull the agent's final `RESULT: {...}` object out of stdout.

    Tolerates code fences and trailing prose: scans every line for a `RESULT:`
    prefix and takes the LAST one that parses as JSON. Falls back to the last
    bare JSON object in the text if no prefixed line parsed. Raises ChangeError
    if nothing usable is found.
    """
    candidates: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip("`").strip()
        idx = line.find("RESULT:")
        if idx != -1:
            candidates.append(line[idx + len("RESULT:"):].strip())
    for chunk in reversed(candidates):
        m = _JSON_OBJ_RE.search(chunk)
        if not m:
            continue
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    # Fallback: last bare {...} anywhere in the output.
    for m in reversed(list(_JSON_OBJ_RE.finditer(text))):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "changed" in obj:
                return obj
        except ValueError:
            continue
    raise ChangeError(
        "el agente de cambios no devolvio una linea RESULT valida: %r" % text[-300:]
    )


def _normalize_result(obj: dict) -> dict:
    """Validate + coerce the agent's RESULT object into the return shape.

    Keeps only real deliverable tags in `changed`; coerces summary/unsupported to
    the right types."""
    raw_changed = obj.get("changed")
    changed: list[str] = []
    if isinstance(raw_changed, list):
        for item in raw_changed:
            tag = str(item).strip().lower()
            if tag in TARGETS and tag not in changed:
                changed.append(tag)
    summary = obj.get("summary")
    summary = str(summary).strip() if summary is not None else ""
    unsupported = obj.get("unsupported")
    if unsupported in (None, "", "null"):
        unsupported = None
    else:
        unsupported = str(unsupported).strip() or None
    return {"changed": changed, "summary": summary, "unsupported": unsupported}


# ---------- publish safety net ----------

def _ensure_published(video_id: str, changed: list[str]) -> None:
    """Guarantee each changed deliverable reached the run's output folder.

    The agent publishes the video itself via the `recompose` primitive (which
    ends in `publish_output`), and the carousel/quote respawn generators publish
    themselves — so this is a best-effort safety net: it only (re)publishes the
    VIDEO from the run's final.mp4 if that file is present and hasn't been moved
    out yet. Carousel/quotes are left to their generators (we can't re-run those
    cheaply here). Never raises."""
    if "video" not in changed:
        return
    import contenido_bionico.pipeline as pl  # lazy: avoids import cycle
    rd = _run_dir(video_id)
    final_run = rd / "final.mp4"
    if not final_run.exists():
        return  # already published (moved to output) or never produced
    try:
        pl.publish_output(video_id, run_filename="final.mp4", kind="short", required=False)
    except Exception as exc:  # noqa: BLE001 - publish is a safety net, never fail the change
        print(f"[change] aviso: publish de respaldo fallo: {exc}", flush=True)


def _is_fresh_file(path: Path, since: float) -> bool:
    """True only for a real file written after this edit attempt started."""
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_size > 0 and st.st_mtime >= since


def _published_video_path(video_id: str) -> Path | None:
    """Return the expected published final video path for a short run."""
    out = _output_dir(video_id)
    if out is None:
        return None
    try:
        import contenido_bionico.pipeline as pl  # lazy: avoids import cycle

        return out / f"final_{pl.run_number(video_id)}.mp4"
    except Exception:  # noqa: BLE001 - malformed run id / config; no recovery
        return None


def _fresh_publish_report(video_id: str, since: float) -> Path | None:
    """Return the published path from a fresh video publish report, if valid."""
    report_path = _run_dir(video_id) / "logs" / "Publish_Report.json"
    if not _is_fresh_file(report_path, since):
        return None
    report = _read_json(report_path)
    if report.get("kind") != "short" or report.get("run_filename") != "final.mp4":
        return None
    if report.get("status") not in {"published", "left_in_run_dir"}:
        return None
    raw_path = report.get("published_path")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    try:
        return path if path.exists() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _publish_fresh_run_final(video_id: str, since: float) -> Path | None:
    """Publish a fresh run-dir final.mp4 that the agent rendered but did not move."""
    src = _run_dir(video_id) / "final.mp4"
    if not _is_fresh_file(src, since):
        return None
    try:
        import contenido_bionico.pipeline as pl  # lazy: avoids import cycle

        return pl.publish_output(
            video_id, run_filename="final.mp4", kind="short", required=True
        )
    except Exception as exc:  # noqa: BLE001 - recovery must stay conservative
        print(f"[change] aviso: no se pudo recuperar final.mp4 fresco: {exc}", flush=True)
        return None


def _fresh_plan_count(video_id: str, target: str, since: float) -> int | None:
    """Expected slide/quote count from the respawn's FRESH plan file.

    The carousel/quote generators write their plan (Carousel_Plan.json /
    Quote_Plan.json) only after every item rendered, so a FRESH plan whose
    count matches the fresh published files proves the respawn completed.
    Returns None when the plan is missing, stale (predates this edit — i.e.
    the fork's copy of the parent's plan), or unreadable."""
    rd = _run_dir(video_id)
    if target == "carousel":
        plan_path, key = rd / "carousel" / "Carousel_Plan.json", "slides"
    elif target == "quotes":
        plan_path, key = rd / "quotes" / "Quote_Plan.json", "quotes"
    else:
        return None
    if not _is_fresh_file(plan_path, since):
        return None
    items = _read_json(plan_path).get(key)
    if isinstance(items, list) and items:
        return len(items)
    return None


def _fresh_deliverables_for_target(video_id: str, target: str, since: float) -> list[Path]:
    """Return fresh deliverables that prove this forked edit completed.

    A fork copies the parent output folder before the agent runs. Therefore this
    checks mtimes against the edit start time so copied parent files are never
    mistaken for a successful change. For carousel/quotes the fresh file count
    must additionally match the fresh plan's slide/quote count — a half-updated
    set (respawn or publish died midway) is never declared a success.
    """
    if target == "video":
        report_path = _fresh_publish_report(video_id, since)
        if report_path is not None:
            return [report_path]
        published = _published_video_path(video_id)
        if published is not None and _is_fresh_file(published, since):
            return [published]
        recovered = _publish_fresh_run_final(video_id, since)
        return [recovered] if recovered is not None else []

    out = _output_dir(video_id)
    if out is None:
        return []
    if target == "carousel":
        pattern = "carrusel_slide*.png"
    elif target == "quotes":
        pattern = "quote_*.mp4"
    else:
        return []
    fresh = sorted(p for p in out.glob(pattern) if _is_fresh_file(p, since))
    expected = _fresh_plan_count(video_id, target, since)
    if expected is None or len(fresh) != expected:
        if fresh:
            print(
                f"[change] recuperacion rechazada para {target}: "
                f"{len(fresh)} archivo(s) fresco(s) vs plan "
                f"{'ausente/viejo' if expected is None else expected}",
                flush=True,
            )
        return []
    return fresh


def _recover_completed_change(
    video_id: str,
    target: str,
    since: float,
    *,
    reason: str,
) -> dict | None:
    """Recover when rendering/publishing finished but the LLM contract failed."""
    fresh = _fresh_deliverables_for_target(video_id, target, since)
    if not fresh:
        return None
    names = ", ".join(p.name for p in fresh[:3])
    if len(fresh) > 3:
        names += f", +{len(fresh) - 3}"
    print(
        f"[change] recuperado: {target} ya fue publicado despues del inicio "
        f"({reason}); archivos: {names}",
        flush=True,
    )
    return {
        "changed": [target],
        "summary": (
            f"Recuperado automaticamente: {target} ya estaba renderizado y publicado."
        ),
        "unsupported": None,
    }


# ---------- orchestration ----------

def _venv_python() -> str:
    """The project venv's python, forward-slashed so the agent's Bash tool can
    run it verbatim on Windows. Falls back to 'python' when no venv exists."""
    root = PIPELINE_DIR.parent
    for candidate in (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate).replace("\\", "/")
    return "python"


def _py_command(py: str, body: str) -> str:
    """One ready-to-run shell command: venv python -c with UTF-8-safe stdout.
    `body` must only use single-quoted Python string literals."""
    return (
        f'{py} -c "import sys; '
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace'); "
        f'{body}"'
    )


def _rerender_command(py: str, part_key: str, vid: str) -> str:
    body = (
        "from contenido_bionico.short.change.primitives import rerender_part; "
        f"rerender_part('{vid}', '{part_key}'); print('OK')"
    )
    return _py_command(py, body)


def _respawn_command(
    py: str, respawn: str, vid: str, anim_opts: dict | None
) -> str | None:
    """The EXACT command for a part's respawn entry, with the version's own
    anim_opts snapshot already folded in. `<NOTAS>` is the only placeholder the
    agent replaces (with the user's request, escaped for a Python
    single-quoted string)."""
    opts_kwarg = f", anim_opts={anim_opts!r}" if anim_opts else ""
    opts_splat = f", **{anim_opts!r}" if anim_opts else ""
    bodies = {
        "run_animate": (
            "import contenido_bionico.pipeline as pl; "
            f"pl.run_animate('{vid}', edit=True, build_extras=False, "
            f"notes='<NOTAS>'{opts_splat}); print('OK')"
        ),
        "surgical_recut": (
            "import contenido_bionico.pipeline as pl; "
            f"pl.surgical_recut('{vid}', '<NOTAS>'{opts_kwarg}); print('OK')"
        ),
        "generate_and_publish_carousel": (
            "import contenido_bionico.pipeline as pl; "
            f"pl.generate_and_publish_carousel('{vid}', strict=True, "
            f"notes='<NOTAS>'); pl.refresh_run_caption('{vid}'); print('OK')"
        ),
        "generate_and_publish_quotes": (
            "import contenido_bionico.pipeline as pl; "
            f"pl.generate_and_publish_quotes('{vid}', strict=True, "
            f"notes='<NOTAS>'); pl.refresh_run_caption('{vid}'); print('OK')"
        ),
    }
    body = bodies.get(respawn)
    return _py_command(py, body) if body else None


def _registry_summary(
    target: str, video_id: str, anim_opts: dict | None = None
) -> dict[str, Any]:
    """Fold the registry's parts for THIS target into a JSON-able map so the
    agent sees the routing table AND the exact venv command per part
    (rerender_cmd / respawn_cmd, plus recompose_cmd and the per-run component
    seed command for video) in its message — the prompt teaches policy, the
    registry data carries the incantations. The agent never reads
    `registry.py`."""
    py = _venv_python()
    vid = str(video_id)
    parts: dict[str, Any] = {}
    for key, part in PARTS.items():
        if part.target != target:
            continue
        parts[key] = {
            "intermediates": list(part.intermediates),
            "rerender": part.rerender,
            "respawn": part.respawn,
            "dependents": list(part.dependents),
            "rerender_cmd": _rerender_command(py, key, vid) if part.rerender else None,
            "respawn_cmd": (
                _respawn_command(py, part.respawn, vid, anim_opts)
                if part.respawn
                else None
            ),
        }
    summary: dict[str, Any] = {"venv_python": py, "parts": parts}
    if target == "video":
        summary["recompose_cmd"] = _py_command(
            py,
            "from contenido_bionico.short.change.primitives import recompose; "
            f"recompose('{vid}'); print('OK')",
        )
        run_dir = str(_run_dir(vid)).replace("\\", "/")
        summary["seed_component_copy_cmd"] = _py_command(
            py,
            "from pathlib import Path; "
            "from contenido_bionico.shared.remotion_manifest_writer import "
            "copy_shared_component_tsx; "
            f"copy_shared_component_tsx(run_id='{vid}', component='<COMPONENTE>', "
            f"run_dir=Path('{run_dir}')); print('OK')",
        )
    return summary


def _build_message(video_id: str, target: str, notes: str, context: dict) -> str:
    rd = _run_dir(video_id)
    return "\n\n".join([
        f"<TARGET>\n{target}\n</TARGET>",
        f"<USER_CHANGE_REQUEST>\n{notes.strip()}\n</USER_CHANGE_REQUEST>",
        f"<RUN_DIR>\n{rd}\n</RUN_DIR>",
        "<RUN_CONTEXT>\n" + json.dumps(context, ensure_ascii=False, indent=2) + "\n</RUN_CONTEXT>",
    ])


_FOLLOWUP_SYSTEM_PROMPT = """\
You are completing an interrupted video-editing agent session. The session did
its work but ended WITHOUT printing the mandatory final RESULT line. Based ONLY
on the transcript tail provided (do not assume anything it does not show),
print that line now. Print EXACTLY one line and nothing else:

RESULT: {"changed": [...], "summary": "<short Spanish>", "unsupported": null}

Rules:
- "changed" may only list deliverables ("video", "carousel", "quotes") the
  transcript SHOWS were actually re-rendered AND published.
- If the transcript shows the change was declined (a `DECLINADO: <razon>`
  line), use "changed": [] and put that Spanish reason in "unsupported".
- If you cannot tell from the transcript, print exactly:
  RESULT: {"changed": [], "summary": "", "unsupported": "no se pudo confirmar el resultado del cambio"}
"""


def _followup_result(
    agent_cmd: str,
    video_id: str,
    target: str,
    transcript_tail: str,
    log_dir: Path,
    started_at: float,
) -> dict | None:
    """ONE cheap, tool-less follow-up turn: ask a small model to emit the
    missing RESULT line from the session's transcript tail. A claimed change is
    only accepted when a fresh deliverable on disk backs it (the follow-up must
    never invent a success). Returns the normalized result dict, or None."""
    try:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        result = run_agent_code(
            agent_cmd=agent_cmd,
            system_prompt=_FOLLOWUP_SYSTEM_PROMPT,
            initial_message=(
                "<TRANSCRIPT_TAIL>\n"
                + (transcript_tail or "")[-6000:]
                + "\n</TRANSCRIPT_TAIL>\n\nEmit the RESULT line now."
            ),
            cwd=PIPELINE_DIR,
            timeout_seconds=FOLLOWUP_TIMEOUT_SECONDS,
            max_turns=2,
            tools=[],
            model=FOLLOWUP_MODEL,
            effort="low",
            permission_mode=None,
            log_path=log_dir / f"{stamp}_short_change_result_followup.log",
            env=AGENT_ENV,
        )
    except Exception as exc:  # noqa: BLE001 - the follow-up is best-effort
        print(f"[change] aviso: el turno de seguimiento para RESULT fallo: {exc}", flush=True)
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = _normalize_result(_parse_result_line(result.stdout))
    except ChangeError:
        return None
    if parsed["changed"] and not _fresh_deliverables_for_target(
        video_id, target, started_at
    ):
        print(
            "[change] aviso: el RESULT del turno de seguimiento afirmaba cambios "
            "sin evidencia fresca en disco; se descarta",
            flush=True,
        )
        return None
    print("[change] RESULT recuperado con un turno de seguimiento", flush=True)
    return parsed


def run_change(video_id: str, target: str, notes: str, anim_opts: dict | None = None) -> dict:
    """Apply the user's free-text change to the (already forked) run.

    Launches the autonomous editor agent, which edits the run's render inputs and
    re-renders the affected deliverable, then parses its `RESULT` line. Returns
    {changed: [deliverable...], summary: str, unsupported: str|None}.

    `anim_opts` (anim_quality + no_* feature flags + anim_count) is folded into the
    RUN_CONTEXT so a re-animation the agent triggers keeps the version's own
    quality/feature snapshot.
    """
    if target not in TARGETS:
        raise ChangeError(f"target invalido: {target!r} (usa video|carousel|quotes)")
    if not (notes or "").strip():
        raise ChangeError("el cambio no puede estar vacio")
    if not PROMPT_PATH.exists():
        raise ChangeError(f"falta el system prompt: {PROMPT_PATH}")
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise ChangeError(missing_agent_cmd_message())

    context = inspect_run(video_id, target)
    # The routing table the agent uses instead of reading registry.py — only the
    # parts for THIS target (a video change never sees carousel/quotes parts),
    # each with its exact ready-to-run venv command.
    context["registry"] = _registry_summary(target, video_id, anim_opts)
    if anim_opts:
        # Surface the version's quality/feature snapshot so the agent forwards it
        # when it re-animates (keeps audio/structure from resetting).
        context["anim_opts"] = anim_opts
    message = _build_message(video_id, target, notes, context)

    rd = _run_dir(video_id)
    log_dir = rd / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_dir / f"{stamp}_short_change_orchestrator.log"
    timeout_seconds = _timeout_seconds_for(context)
    # IRON LAW integrity snapshot: pipeline source must be byte-identical after
    # the session (see _fail_if_source_tree_dirty).
    source_snapshot = _snapshot_source_tree()
    started_at = time.time()

    # The agent's Bash tool caps each command at 2 min by default (10 min hard
    # max) — far below the registry's single atomic respawn/rerender commands
    # (one Remotion render alone is budgeted 30 min). Raise both caps to this
    # session's wall-clock budget so foreground commands can run to completion.
    bash_timeout_ms = str(int(timeout_seconds) * 1000)
    agent_env = {
        **AGENT_ENV,
        "BASH_DEFAULT_TIMEOUT_MS": bash_timeout_ms,
        "BASH_MAX_TIMEOUT_MS": bash_timeout_ms,
    }

    def _heartbeat(info: HeartbeatInfo) -> None:
        # One short line to OUR stdout (the job log) so the engine's 30-min
        # log-stall watchdog sees activity while the agent works silently
        # (its Bash/stream output goes to sidecar logs, never the job log).
        elapsed_min = int(info.elapsed_seconds // 60)
        if info.last_status_detail:
            detail = info.last_status_detail
            if len(detail) > 80:
                detail = detail[:77] + "..."
            last_block = f'ultimo: "{detail}"'
        elif info.last_event_type:
            last_block = f"ultimo evento: {info.last_event_type} ({info.event_count} total)"
        else:
            last_block = "esperando al agente"
        print(
            f"[change] agente activo | {elapsed_min} min de {timeout_seconds // 60} | {last_block}",
            flush=True,
        )

    try:
        result = run_agent_code(
            agent_cmd=agent_cmd,
            system_prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            initial_message=message,
            cwd=PIPELINE_DIR,
            timeout_seconds=timeout_seconds,
            max_turns=MAX_TURNS,
            tools=AGENT_TOOLS,
            model=MODEL,
            effort=EFFORT,
            permission_mode="bypassPermissions",
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            heartbeat_callback=_heartbeat,
            log_path=log_path,
            # Merged OVER the runner's SANITIZED child env: forces UTF-8 in the
            # agent's `python -c` children and raises the Bash tool's per-command
            # timeout. Never pass os.environ here — the runner deliberately
            # strips harness vars (CLAUDE_CODE_*/CLAUDECODE/ANTHROPIC_*) so the
            # sub-session doesn't behave as a nested claude and no-op.
            env=agent_env,
        )
    except subprocess.TimeoutExpired as exc:
        _fail_if_source_tree_dirty(source_snapshot)
        recovered = _recover_completed_change(
            video_id, target, started_at, reason="timeout sin RESULT"
        )
        if recovered is not None:
            return recovered
        # The inner agent hit the wall-clock cap and its subprocess tree was killed.
        # Surface a clear, human error (not a raw TimeoutExpired repr) so the
        # watcher -> agent -> cloud chain reports a meaningful failure instead of
        # a stack trace — including the LAST progress marker so the operator sees
        # how far the edit got before the wall.
        raw_output = getattr(exc, "output", None)
        if isinstance(raw_output, bytes):
            raw_output = raw_output.decode("utf-8", errors="replace")
        partial = _partial_progress(
            _session_transcript_text(log_path, raw_output if isinstance(raw_output, str) else None)
        )
        detail = f" (ultimo progreso: {partial})" if partial else ""
        raise ChangeError(
            "el cambio excedio el limite de %d minutos y fue detenido%s; "
            "reintenta con un cambio mas acotado o revisa el log del agente"
            % (timeout_seconds // 60, detail)
        ) from exc

    _fail_if_source_tree_dirty(source_snapshot)

    if result.returncode != 0:
        recovered = _recover_completed_change(
            video_id, target, started_at, reason=f"exit {result.returncode}"
        )
        if recovered is not None:
            return recovered
        decline = _detect_decline(
            _session_transcript_text(log_path, result.stdout, result.stderr)
        )
        if decline is not None:
            print(f"[change] cambio rechazado: {decline}", flush=True)
            return {"changed": [], "summary": "", "unsupported": decline}
        tail = (result.stdout + "\n" + result.stderr).strip().splitlines()
        raise ChangeError(
            "el agente de cambios fallo (exit %s): %s"
            % (result.returncode, " | ".join(tail[-3:])[:400])
        )

    try:
        parsed = _normalize_result(_parse_result_line(result.stdout))
    except ChangeError:
        recovered = _recover_completed_change(
            video_id, target, started_at, reason="salida sin RESULT"
        )
        if recovered is not None:
            return recovered
        transcript = _session_transcript_text(log_path, result.stdout)
        followup = _followup_result(
            agent_cmd, video_id, target, transcript, log_dir, started_at
        )
        if followup is not None:
            parsed = followup
        else:
            decline = _detect_decline(transcript)
            if decline is not None:
                print(f"[change] cambio rechazado: {decline}", flush=True)
                return {"changed": [], "summary": "", "unsupported": decline}
            raise
    _ensure_published(video_id, parsed["changed"])
    return parsed
