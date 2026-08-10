"""Provider-neutral subprocess launcher for AI agent calls."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from typing import Callable

from contenido_bionico.shared import config
from contenido_bionico.shared.runtime.claude_runner import (
    ClaudeResult,
    HeartbeatInfo,
    _append_debug_summary,
    _sidecar_path,
    _write_command_metadata,
    load_env,
    printable_command,
    resolve_claude_cmd,
    run_claude_code,
)
from contenido_bionico.shared.runtime.runner_common import (
    NO_WINDOW,
    ProgressState,
    StreamDrainer,
    close_quietly,
    kill_process_tree,
    open_log_files,
    parse_stream_event,
    send_stdin_async,
    start_heartbeat,
    start_process,
    wait_or_kill_tree,
)


_CUSTOM_PROVIDER_ERROR = (
    "El proveedor de IA 'custom' requiere configuracion manual y no esta "
    "implementado en esta instalacion. Ejecuta `contenido-bionico setup` y "
    "elige Claude o ChatGPT, o prepara tu proveedor personalizado antes de "
    "volver a intentarlo."
)

# Knobs that codex genuinely lacks get ONE printed notice per process instead
# of being silently dropped (or silently spamming every retry).
_NOTICED: set[str] = set()

# Pipeline-wide default for max_turns. Call sites always plumb an int through,
# so the max_turns notice only fires when a caller explicitly set a value
# DIFFERENT from this default — passing the default along is not a request to
# limit turns and used to spam the notice on every codex runner process.
DEFAULT_MAX_TURNS = 300

# The agents' tool lists are Claude tool names; for codex they only decide
# whether the session may write inside the workspace.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# `codex exec` calls the same knob `model_reasoning_effort` and accepts
# minimal/low/medium/high; Claude's top levels don't exist there, so they
# degrade to codex's universally-supported maximum instead of failing the run.
_CODEX_EFFORT_MAP = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _notice_once(key: str, message: str) -> None:
    if key in _NOTICED:
        return
    _NOTICED.add(key)
    print(f"[agent-runner] aviso: {message}", flush=True)


def resolve_codex_cmd() -> str | None:
    if os.name == "nt":
        cmd = shutil.which("codex.cmd")
        if cmd:
            return cmd
    return shutil.which("codex")


def resolve_agent_cmd(provider: str | None = None) -> str | None:
    selected = provider or config.agent_provider()
    if selected == "claude_code":
        return resolve_claude_cmd()
    if selected == "codex_cli":
        return resolve_codex_cmd()
    if selected == "custom":
        # Fail fast with a clear message instead of letting the caller hit a
        # generic "CLI not found" later.
        raise RuntimeError(_CUSTOM_PROVIDER_ERROR)
    return None


# CLI binary per configured provider, for user-facing error messages.
_PROVIDER_CLI_NAMES = {"claude_code": "claude", "codex_cli": "codex"}


def missing_agent_cmd_message(provider: str | None = None) -> str:
    """Spanish, actionable message for when resolve_agent_cmd() returns None.

    Names the CLI of the CONFIGURED provider so the user knows exactly what
    to install (or that they should re-run setup to pick another provider).
    """
    selected = provider or config.agent_provider()
    cli = _PROVIDER_CLI_NAMES.get(selected, selected)
    return (
        f"No encuentro el CLI del proveedor de IA configurado ('{cli}'). "
        f"Instalalo y asegurate de que '{cli}' este en el PATH, o ejecuta "
        "'contenido-bionico setup' para elegir otro proveedor."
    )


def _codex_prompt(system_prompt: str, initial_message: str) -> str:
    return "\n\n".join(
        [
            "<SYSTEM_PROMPT>",
            system_prompt.rstrip(),
            "</SYSTEM_PROMPT>",
            "<USER_MESSAGE>",
            initial_message.rstrip(),
            "</USER_MESSAGE>",
        ]
    )


def _codex_event_type(event: dict) -> str | None:
    """Event type across codex --json formats: top-level `type` (new) or
    the wrapped `msg.type` (older releases)."""
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type:
        return event_type
    msg = event.get("msg")
    if isinstance(msg, dict):
        msg_type = msg.get("type")
        if isinstance(msg_type, str) and msg_type:
            return msg_type
    return None


def run_codex_exec(
    *,
    codex_cmd: str,
    system_prompt: str,
    initial_message: str,
    cwd: Path,
    timeout_seconds: int | None,
    max_turns: int = DEFAULT_MAX_TURNS,
    system_prompt_mode: str = "append",
    tools: list[str] | None = None,
    model: str | None = None,
    effort: str = "medium",
    permission_mode: str | None = None,
    heartbeat_interval_seconds: float | None = None,
    heartbeat_callback: Callable[[HeartbeatInfo], None] | None = None,
    stall_threshold_seconds: float = 300.0,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> ClaudeResult:
    """Run one `codex exec` call with the same supervision interface as
    `run_claude_code` (timeout, heartbeat + stall detection, sidecar logs).

    Mapping of the shared knobs onto what codex actually supports:
    - `tools` decides the sandbox: agents that need Write/Edit run with
      `--sandbox workspace-write` (writes confined to `cwd`), everyone else
      stays read-only. Codex has no per-tool allowlist beyond that.
    - `effort` maps to `model_reasoning_effort` (see _CODEX_EFFORT_MAP).
    - claude-* `model` names can't run on codex: noted once, codex uses its
      own default model.
    - `max_turns` and `permission_mode` have no codex equivalent: noted once
      instead of silently dropped (`max_turns` only when set to something
      other than DEFAULT_MAX_TURNS). The system prompt is always embedded in
      the stdin message (codex can't replace its base prompt), so
      `system_prompt_mode="replace"` is approximated and noted once.
    """
    if system_prompt_mode not in {"append", "replace"}:
        raise ValueError(f"unknown system_prompt_mode: {system_prompt_mode}")

    needs_write = bool(tools) and any(t in _WRITE_TOOLS for t in tools)
    sandbox = "workspace-write" if needs_write else "read-only"

    if max_turns is not None and max_turns != DEFAULT_MAX_TURNS:
        _notice_once(
            "codex:max_turns",
            "codex CLI no soporta limitar turnos (max_turns); se ignora.",
        )
    if permission_mode:
        _notice_once(
            "codex:permission_mode",
            "codex CLI no usa permission_mode; el sandbox se elige segun las "
            f"tools del agente (esta llamada: '{sandbox}').",
        )
    if system_prompt_mode == "replace":
        _notice_once(
            "codex:system_prompt_mode",
            "codex CLI no permite reemplazar su prompt base; el system prompt "
            "se antepone al mensaje del agente.",
        )
    codex_model = model
    if model and model.startswith("claude-"):
        _notice_once(
            f"codex:model:{model}",
            f"el modelo '{model}' es de Claude; codex usara su modelo por defecto.",
        )
        codex_model = None
    codex_effort = _CODEX_EFFORT_MAP.get(effort) if effort else None
    if effort and codex_effort is None:
        _notice_once(
            f"codex:effort:{effort}",
            f"codex CLI no reconoce el nivel de esfuerzo '{effort}'; se omite.",
        )

    with tempfile.TemporaryDirectory(prefix="contenido-bionico-codex-") as tmp:
        output_path = Path(tmp) / "last-message.txt"
        stream_path = _sidecar_path(log_path, ".stream.jsonl")
        stderr_path = _sidecar_path(log_path, ".stderr.log")
        debug_path = _sidecar_path(log_path, ".debug.log")
        command_path = _sidecar_path(log_path, ".command.json")
        cmd = [
            codex_cmd,
            "exec",
            "--cd",
            str(cwd),
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "--output-last-message",
            str(output_path),
        ]
        if codex_model:
            cmd.extend(["--model", codex_model])
        if codex_effort:
            cmd.extend(["-c", f"model_reasoning_effort={codex_effort}"])
        cmd.append("-")

        start_ts = datetime.now()
        _write_command_metadata(
            path=command_path,
            provider="codex_cli",
            command=cmd,
            cwd=cwd,
            start_ts=start_ts,
            timeout_seconds=timeout_seconds,
            model=model,
            tools=tools,
        )

        stream_fp, stderr_fp = open_log_files(stream_path, stderr_path)

        # codex builds no child_env of its own, so a caller overlay (e.g.
        # PYTHONUTF8/PYTHONIOENCODING) merges over the inherited os.environ.
        # Default None keeps the inherit-everything behavior existing callers
        # rely on.
        proc_env = {**os.environ, **env} if env else None
        proc = start_process(cmd, cwd=cwd, env=proc_env)
        send_stdin_async(proc, _codex_prompt(system_prompt, initial_message))

        progress = ProgressState()
        start_monotonic = time.monotonic()

        def _on_stdout_line(line: str) -> None:
            nbytes = len(line.encode("utf-8", errors="replace"))
            now = time.monotonic()
            with progress.lock:
                progress.stdout_bytes += nbytes
                progress.last_stdout_ts = now
            event = parse_stream_event(line)
            if event is None:
                return
            event_type = _codex_event_type(event)
            with progress.lock:
                progress.event_count += 1
                progress.last_event_type = event_type
                progress.last_event_ts = now

        def _on_stderr_chunk(chunk: str) -> None:
            # Bytes only: stderr noise must not count as liveness.
            nbytes = len(chunk.encode("utf-8", errors="replace"))
            with progress.lock:
                progress.stderr_bytes += nbytes

        out_drain = StreamDrainer(
            proc.stdout, mode="lines", sink_fp=stream_fp, on_data=_on_stdout_line
        )
        err_drain = StreamDrainer(
            proc.stderr, mode="chunks", sink_fp=stderr_fp, on_data=_on_stderr_chunk
        )
        out_drain.start()
        err_drain.start()

        heartbeat_stop, heartbeat_thread = start_heartbeat(
            progress=progress,
            start_monotonic=start_monotonic,
            interval_seconds=heartbeat_interval_seconds,
            callback=heartbeat_callback,
            stall_threshold_seconds=stall_threshold_seconds,
        )

        timed_out = wait_or_kill_tree(proc, timeout_seconds)

        # Same post-exit sweep as run_claude_code: backgrounded children that
        # inherited the pipes would otherwise wedge the drain threads.
        try:
            kill_process_tree(proc)
        except Exception:
            pass

        out_drain.join(timeout=15)
        err_drain.join(timeout=15)

        if heartbeat_thread is not None:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=5)

        close_quietly(proc.stdin, proc.stdout, proc.stderr)

        end_ts = datetime.now()
        close_quietly(stream_fp, stderr_fp)

        stdout = out_drain.text()
        if output_path.exists():
            final_message = output_path.read_text(encoding="utf-8", errors="replace")
            if final_message.strip():
                stdout = final_message
        stderr = err_drain.text()

        if timed_out:
            _append_debug_summary(
                path=debug_path,
                started_at=start_ts,
                ended_at=end_ts,
                returncode=proc.returncode,
                timed_out=True,
                stdout_bytes=progress.stdout_bytes,
                stderr_bytes=progress.stderr_bytes,
                extra="status: subprocess timeout",
            )
            raise subprocess.TimeoutExpired(
                cmd,
                timeout_seconds,
                output=stdout,
                stderr=stderr,
            )

        _append_debug_summary(
            path=debug_path,
            started_at=start_ts,
            ended_at=end_ts,
            returncode=proc.returncode,
            timed_out=False,
            stdout_bytes=progress.stdout_bytes,
            stderr_bytes=progress.stderr_bytes,
        )

        return ClaudeResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            command=cmd,
            start_ts=start_ts,
            end_ts=end_ts,
        )


def run_agent_code(
    *,
    agent_cmd: str,
    system_prompt: str,
    initial_message: str,
    cwd: Path,
    timeout_seconds: int | None,
    max_turns: int = DEFAULT_MAX_TURNS,
    system_prompt_mode: str = "append",
    tools: list[str] | None = None,
    model: str | None = None,
    effort: str = "medium",
    permission_mode: str | None = None,
    heartbeat_interval_seconds: float | None = None,
    heartbeat_callback: Callable[[HeartbeatInfo], None] | None = None,
    stall_threshold_seconds: float = 300.0,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> ClaudeResult:
    selected = config.agent_provider()
    if selected == "claude_code":
        return run_claude_code(
            claude_cmd=agent_cmd,
            system_prompt=system_prompt,
            initial_message=initial_message,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            system_prompt_mode=system_prompt_mode,
            tools=tools,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_callback=heartbeat_callback,
            stall_threshold_seconds=stall_threshold_seconds,
            log_path=log_path,
            env=env,
        )
    if selected == "codex_cli":
        return run_codex_exec(
            codex_cmd=agent_cmd,
            system_prompt=system_prompt,
            initial_message=initial_message,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            system_prompt_mode=system_prompt_mode,
            tools=tools,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            heartbeat_callback=heartbeat_callback,
            stall_threshold_seconds=stall_threshold_seconds,
            log_path=log_path,
            env=env,
        )
    if selected == "custom":
        raise RuntimeError(_CUSTOM_PROVIDER_ERROR)
    raise RuntimeError(f"unsupported agent provider: {selected}")
