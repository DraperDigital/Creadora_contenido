"""Shared Claude Code subprocess launcher for contenido-bionico workflows."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from contenido_bionico.shared import config
from contenido_bionico.shared.runtime.runner_common import (
    NO_WINDOW,
    HeartbeatInfo,
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


_CLAUDE_HELP_CACHE: dict[str, str] = {}


class ClaudeUsageLimitError(RuntimeError):
    """The Claude CLI refused/aborted because the subscription usage limit is
    exhausted (5-hour or weekly window). Callers should treat this as "defer
    and retry after the window resets", never as a normal failure.

    Detection also drops a sentinel file (BIONICO_LIMIT_MARKER when set, else
    `limit_reached.marker` in the process cwd) so the engine's jobrunner can
    map a nonzero pipeline exit to its quota-defer path (exit code 75) even if
    an intermediate layer swallowed this exception.
    """


# Messages the Claude CLI prints when the subscription window is exhausted.
# Deliberately narrow: transient 429/529 rate limits must NOT match (they are
# retried in place), only the hard usage-limit refusals.
_USAGE_LIMIT_RE = re.compile(
    r"usage limit reached"
    r"|usage_limit_reached"
    r"|claude (?:ai|max) usage limit"
    r"|you'?ve reached your(?: \w+)? usage limit"
    r"|hit your usage limit"
    r"|5-hour limit reached"
    r"|weekly limit reached"
    r"|out of extra usage",
    re.IGNORECASE,
)


def _limit_marker_path() -> Path:
    override = os.environ.get("BIONICO_LIMIT_MARKER")
    return Path(override) if override else Path.cwd() / "limit_reached.marker"


def _raise_if_usage_limit(returncode: int | None, stdout: str, stderr: str) -> None:
    """On a nonzero CLI exit that looks like a usage-limit refusal: write the
    sentinel marker and raise ClaudeUsageLimitError."""
    if not returncode:  # 0 or None: the call worked; content mentioning limits is fine
        return
    blob = "\n".join(x for x in (stdout, stderr) if x)[-20000:]
    if not _USAGE_LIMIT_RE.search(blob):
        return
    marker = _limit_marker_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
        )
    except OSError:
        pass
    raise ClaudeUsageLimitError(
        "Claude usage limit reached (the CLI refused to run); "
        "retry after the usage window resets"
    )


@dataclass(frozen=True)
class ClaudeResult:
    returncode: int
    stdout: str
    stderr: str
    command: list[str]
    start_ts: datetime
    end_ts: datetime


@dataclass
class _ParsedStreamState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    assistant_texts: list[str] = field(default_factory=list)
    result_text: str | None = None


def _sidecar_path(log_path: Path | None, suffix: str) -> Path | None:
    if log_path is None:
        return None
    return log_path.with_suffix(suffix)


def _safe_write_text(path: Path | None, text: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _safe_append_text(path: Path | None, text: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(text)
    except OSError:
        pass


_REDACTED_VALUE_FLAGS = (
    "--append-system-prompt",
    "--append-system-prompt-file",
    "--system-prompt",
    "--system-prompt-file",
    "--debug-file",
    "--mcp-config",
    "--settings",
)


def _redact_command(command: list[str]) -> list[str]:
    out = command[:]
    for i, token in enumerate(out):
        if token in _REDACTED_VALUE_FLAGS and i + 1 < len(out):
            out[i + 1] = f"<{token.lstrip('-')} omitted>"
    out.append("<stdin initial message omitted>")
    return out


def _write_command_metadata(
    *,
    path: Path | None,
    provider: str,
    command: list[str],
    cwd: Path,
    start_ts: datetime,
    timeout_seconds: int | None,
    model: str | None,
    tools: list[str] | None,
) -> None:
    if path is None:
        return
    payload = {
        "provider": provider,
        "started_at": start_ts.isoformat(timespec="seconds"),
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "model": model,
        "tools": tools,
        "command": _redact_command(command),
    }
    _safe_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _append_debug_summary(
    *,
    path: Path | None,
    started_at: datetime,
    ended_at: datetime,
    returncode: int | None,
    timed_out: bool,
    stdout_bytes: int,
    stderr_bytes: int,
    extra: str = "",
) -> None:
    if path is None:
        return
    lines = [
        "",
        "=== contenido-bionico runner summary ===",
        f"started_at: {started_at.isoformat(timespec='seconds')}",
        f"ended_at: {ended_at.isoformat(timespec='seconds')}",
        f"duration_seconds: {(ended_at - started_at).total_seconds():.3f}",
        f"returncode: {returncode}",
        f"timed_out: {timed_out}",
        f"stdout_bytes: {stdout_bytes}",
        f"stderr_bytes: {stderr_bytes}",
    ]
    if extra:
        lines.append(extra.rstrip())
    lines.append("")
    _safe_append_text(path, "\n".join(lines))


def _claude_help(claude_cmd: str) -> str:
    cached = _CLAUDE_HELP_CACHE.get(claude_cmd)
    if cached is not None:
        return cached
    try:
        out = subprocess.check_output(
            [claude_cmd, "--help"],
            timeout=15,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )
    except Exception:
        out = ""
    _CLAUDE_HELP_CACHE[claude_cmd] = out
    return out


# Flags that protect the agent's isolation (no user MCP servers, no skills,
# no user settings). When the --help probe is inconclusive these are treated
# as SUPPORTED and passed anyway: a hard CLI error is better than silently
# running an agent without isolation.
_ISOLATION_FLAGS = {
    "--strict-mcp-config",
    "--mcp-config",
    "--disable-slash-commands",
    "--setting-sources",
}


def _flag_pattern(flag: str) -> re.Pattern[str]:
    """Regex that finds `flag` in help text, including bracketed forms.

    Claude's help lists some flags combined, e.g. `--append-system-prompt[-file]`
    or `--debug[-file]`, which a literal substring search misses. Each `-`
    joining the flag's segments may appear as `-`, `[-` or `-]`, and a closing
    `]` may trail the final segment. The lookahead rejects longer flags (so
    probing `--debug` does not match a help that only lists `--debug-file`)
    while still accepting a `[` that marks an optional suffix.
    """
    tokens = [re.escape(t) for t in flag.lstrip("-").split("-")]
    body = r"\[?-\]?".join(tokens)
    return re.compile(r"--" + body + r"\]?(?![\w-])")


def _claude_supports(claude_cmd: str, flag: str) -> bool:
    help_text = _claude_help(claude_cmd)
    if not help_text.strip():
        # Probe inconclusive (`claude --help` unavailable): keep the
        # isolation-critical flags, drop the merely-nice-to-have ones.
        return flag in _ISOLATION_FLAGS
    return _flag_pattern(flag).search(help_text) is not None


def load_env(root: Path) -> None:
    """Load `<root>/.env` into os.environ without overriding existing vars.

    Mirrors `shared.config.read_env_file` semantics (surrounding quotes are
    stripped, comment lines are skipped) so a quoted value can't poison the
    environment just because it entered through this path. Additionally
    tolerates an `export KEY=...` prefix and trailing inline comments on
    unquoted values.
    """
    env_path = Path(root) / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        else:
            # Trailing inline comment (only meaningful on unquoted values).
            cut = min(
                (idx for idx in (value.find(" #"), value.find("\t#")) if idx != -1),
                default=-1,
            )
            if cut != -1:
                value = value[:cut].rstrip()
        if key:
            os.environ.setdefault(key, value)


def resolve_claude_cmd() -> str | None:
    cmd = shutil.which("claude")
    if not cmd:
        return None
    cmd_path = Path(cmd)
    if os.name == "nt" and cmd_path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        native_exe = (
            cmd_path.parent
            / "node_modules"
            / "@anthropic-ai"
            / "claude-code"
            / "bin"
            / "claude.exe"
        )
        if native_exe.exists():
            return str(native_exe)
    return cmd


def _claude_subscription_auth() -> bool:
    """True when local config says Claude runs on subscription (session) auth."""
    try:
        cfg = config.load_config()
    except Exception:
        return False
    return (
        str(cfg.get("agent_provider") or "claude_code") == "claude_code"
        and str(cfg.get("agent_engine") or "subscription") == "subscription"
    )


def run_claude_code(
    *,
    claude_cmd: str,
    system_prompt: str,
    initial_message: str,
    cwd: Path,
    timeout_seconds: int | None,
    max_turns: int = 300,
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
    if system_prompt_mode not in {"append", "replace"}:
        raise ValueError(f"unknown system_prompt_mode: {system_prompt_mode}")

    with tempfile.TemporaryDirectory(prefix="contenido-bionico-claude-") as tmp:
        stream_path = _sidecar_path(log_path, ".stream.jsonl")
        debug_path = _sidecar_path(log_path, ".debug.log")
        stderr_path = _sidecar_path(log_path, ".stderr.log")
        command_path = _sidecar_path(log_path, ".command.json")
        prompt_path = Path(tmp) / "system-prompt.md"
        prompt_path.write_text(system_prompt, encoding="utf-8")
        empty_mcp_path = Path(tmp) / "empty-mcp.json"
        empty_mcp_path.write_text('{"mcpServers": {}}', encoding="utf-8")
        prompt_flag = (
            "--system-prompt-file"
            if system_prompt_mode == "replace"
            else "--append-system-prompt-file"
        )
        cmd = [
            claude_cmd,
            "--print",
            "--max-turns",
            str(max_turns),
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "text",
            prompt_flag,
            str(prompt_path),
        ]
        if permission_mode:
            cmd[2:2] = ["--permission-mode", permission_mode]
        # Claude Code treats `--tools ""` as "disable all tools". The
        # stdout-only agents use injected input and must not enter tool mode.
        if tools is not None:
            cmd.extend(["--tools", ",".join(tools)])
        if model:
            cmd.extend(["--model", model])
        for flag in ("--include-partial-messages", "--include-hook-events"):
            if _claude_supports(claude_cmd, flag):
                cmd.append(flag)
        if _claude_supports(claude_cmd, "--debug"):
            cmd.append("--debug")
        if debug_path is not None and _claude_supports(claude_cmd, "--debug-file"):
            cmd.extend(["--debug-file", str(debug_path)])
        if _claude_supports(claude_cmd, "--strict-mcp-config"):
            cmd.append("--strict-mcp-config")
        if _claude_supports(claude_cmd, "--mcp-config"):
            cmd.extend(["--mcp-config", str(empty_mcp_path)])
        if _claude_supports(claude_cmd, "--disable-slash-commands"):
            cmd.append("--disable-slash-commands")
        if _claude_supports(claude_cmd, "--setting-sources"):
            cmd.extend(["--setting-sources", "project"])
        if _claude_supports(claude_cmd, "--effort"):
            cmd.extend(["--effort", effort])

        start_ts = datetime.now()
        _write_command_metadata(
            path=command_path,
            provider="claude_code",
            command=cmd,
            cwd=cwd,
            start_ts=start_ts,
            timeout_seconds=timeout_seconds,
            model=model,
            tools=tools,
        )

        stream_fp, stderr_fp = open_log_files(stream_path, stderr_path)

        # Raise the Claude Code CLI's per-call output ceiling to Opus 4.7's
        # native 128k. The CLI's undocumented default is 64k, which the
        # Remotion author hit (seg 12 of run 14 came back with `stop_reason:
        # max_tokens` and `modelUsage.maxOutputTokens: 64000`). The env var
        # name is present in the claude binary's strings (verified with
        # `strings $(which claude) | grep CLAUDE_CODE_MAX_OUTPUT_TOKENS`).
        child_env = os.environ.copy()
        child_env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "128000")
        # Run the agent as a CLEAN standalone `claude`, stripping Claude Code
        # harness env. If the stack was launched from inside Claude Code (e.g.
        # the watcher started from a Claude session), inherited CLAUDE_CODE_* /
        # CLAUDECODE vars make the child `claude` behave as if nested -- it looks
        # in app-mode config dirs and silently skips its real work (agents exit 0
        # but never Write their Scene.tsx, so every scene then fails to render).
        # Keep only the output-token override; strip everything else
        # harness-specific so the child uses the standalone CLI login.
        _keep = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS"}
        for _k in [k for k in child_env if k.startswith("CLAUDE_CODE_") and k not in _keep]:
            child_env.pop(_k, None)
        for _k in ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT",
                   "AI_AGENT", "BAGGAGE", "ANTHROPIC_BASE_URL"):
            child_env.pop(_k, None)
        # With subscription auth (setup: agent_engine=subscription) a stale
        # ANTHROPIC_API_KEY loaded from .env would make the CLI bill the API
        # instead of using the user's logged-in Claude session. Drop it from
        # the child env only; the parent process and .env stay untouched.
        if _claude_subscription_auth():
            child_env.pop("ANTHROPIC_API_KEY", None)
        # Caller-supplied overlay (e.g. PYTHONUTF8/PYTHONIOENCODING from the
        # change agent) LAYERS OVER the runner's own child_env — it never
        # replaces it, so the isolation setup above (token ceiling, stripped
        # harness vars) is preserved. Default None leaves child_env as-is,
        # keeping every existing caller byte-for-byte unchanged.
        if env:
            child_env.update(env)

        # Dynamic AI Provider & Model Selection
        env_from_file = {}
        root_env_path = Path(__file__).resolve().parents[4] / ".env"
        if root_env_path.exists():
            try:
                for line in root_env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_from_file[k.strip()] = v.strip()
            except Exception:
                pass

        ai_provider = (os.environ.get("AI_PROVIDER") or env_from_file.get("AI_PROVIDER") or "auto").lower()
        ai_model_choice = (os.environ.get("AI_MODEL_CHOICE") or env_from_file.get("AI_MODEL_CHOICE") or "").strip()
        free_llm_key = (os.environ.get("FREE_LLM_API_KEY") or env_from_file.get("FREE_LLM_API_KEY") or "").strip()
        free_llm_url = (os.environ.get("FREE_LLM_BASE_URL") or env_from_file.get("FREE_LLM_BASE_URL") or "https://api.freellmapi.com/v1").strip()
        openrouter_key = (os.environ.get("OPENROUTER_API_KEY") or env_from_file.get("OPENROUTER_API_KEY") or "").strip()
        anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or env_from_file.get("ANTHROPIC_API_KEY") or "").strip()

        active_provider = "Claude Suscripción"
        active_model = model or "claude-3-5-sonnet"

        if ai_provider == "freellm" or (ai_provider == "auto" and free_llm_key):
            if free_llm_key:
                child_env["ANTHROPIC_BASE_URL"] = free_llm_url
                child_env["ANTHROPIC_API_KEY"] = free_llm_key
                active_provider = "FreeLLMAPI"
                active_model = ai_model_choice if (ai_model_choice and ai_model_choice != "default") else "gpt-4o-mini"
        elif ai_provider == "openrouter" or (ai_provider == "auto" and openrouter_key):
            if openrouter_key:
                child_env["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api/v1"
                child_env["ANTHROPIC_API_KEY"] = openrouter_key
                active_provider = "OpenRouter"
                active_model = ai_model_choice if (ai_model_choice and ai_model_choice != "default") else "anthropic/claude-3.5-sonnet"
        elif ai_provider == "anthropic" or (ai_provider == "auto" and anthropic_key):
            if anthropic_key:
                child_env["ANTHROPIC_API_KEY"] = anthropic_key
                active_provider = "Anthropic API"
                if ai_model_choice and ai_model_choice != "default":
                    active_model = ai_model_choice
        elif _claude_subscription_auth():
            child_env.pop("ANTHROPIC_API_KEY", None)
            active_provider = "Claude Suscripción"
            if ai_model_choice and ai_model_choice != "default":
                active_model = ai_model_choice

        if active_model and active_model != model:
            if "--model" in cmd:
                m_idx = cmd.index("--model")
                if m_idx + 1 < len(cmd):
                    cmd[m_idx + 1] = active_model
            else:
                cmd.extend(["--model", active_model])

        stream_fp.write(f"[pipeline] active_ai: provider={active_provider} model={active_model}\n")
        stream_fp.flush()

        proc = start_process(cmd, cwd=cwd, env=child_env)

        # Send the initial message in a side thread so a broken-pipe (claude
        # exiting before we finish writing) cannot interrupt the main flow.
        send_stdin_async(proc, initial_message)

        progress = ProgressState()
        parsed = _ParsedStreamState()
        start_monotonic = time.monotonic()

        def _handle_event(event: dict) -> None:
            event_type = event.get("type")
            with progress.lock:
                progress.event_count += 1
                progress.last_event_type = event_type
                progress.last_event_ts = time.monotonic()
                if (
                    event_type == "system"
                    and event.get("subtype") == "post_turn_summary"
                ):
                    detail = event.get("status_detail")
                    if isinstance(detail, str) and detail.strip():
                        progress.last_status_detail = detail.strip()
            if (
                event_type == "assistant"
                and event.get("subtype") != "partial_message"
                and not event.get("partial")
            ):
                message = event.get("message") or {}
                content = message.get("content") or []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if isinstance(text, str) and text:
                            with parsed.lock:
                                parsed.assistant_texts.append(text)
            elif event_type == "result":
                result_value = event.get("result")
                if isinstance(result_value, str):
                    with parsed.lock:
                        parsed.result_text = result_value

        def _on_stdout_line(line: str) -> None:
            nbytes = len(line.encode("utf-8", errors="replace"))
            now = time.monotonic()
            with progress.lock:
                progress.stdout_bytes += nbytes
                progress.last_stdout_ts = now
            event = parse_stream_event(line)
            if event is not None:
                _handle_event(event)

        def _on_stderr_chunk(chunk: str) -> None:
            # Bytes only: stderr is debug noise and must NOT count as
            # liveness, or a stuck run spamming debug lines never stalls.
            nbytes = len(chunk.encode("utf-8", errors="replace"))
            with progress.lock:
                progress.stderr_bytes += nbytes

        # Drain stdout/stderr in dedicated threads so claude.exe never blocks
        # on full pipe buffers. Only a bounded tail stays in memory; the full
        # streams are teed to the sidecar logs.
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

        # Whether claude.exe exited cleanly or we just killed it, sweep up
        # any remaining children. Backgrounded bash subshells from
        # `run_in_background: true` Bash tool calls (or leaked PowerShell
        # BashOutput sidecars) inherit the stdout/stderr pipe handles and
        # would otherwise keep the drain threads blocked forever.
        try:
            kill_process_tree(proc)
        except Exception:
            pass

        # Pipes should now EOF. Bound the drain join so a stubborn handle
        # cannot wedge the watcher; daemon threads cleaned up on exit.
        out_drain.join(timeout=15)
        err_drain.join(timeout=15)

        if heartbeat_thread is not None:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=5)

        close_quietly(proc.stdout, proc.stderr, proc.stdin)

        end_ts = datetime.now()
        close_quietly(stream_fp, stderr_fp)

        # Assemble the agent's final text from parsed stream-json events.
        # Preference order:
        #   1. `result` event's `result` field — Claude Code's canonical
        #      "final response" string for a `--print` call.
        #   2. Concatenation of every `assistant` text block we saw — covers
        #      runs where the result envelope is missing (e.g. mid-stream
        #      timeout) but we still received content.
        #   3. Raw stdout tail — last-resort fallback so we never lose the
        #      bytes the operator paid for, even if parsing failed.
        with parsed.lock:
            result_text = parsed.result_text
            assistant_text = "".join(parsed.assistant_texts)
        if result_text:
            final_stdout = result_text
        elif assistant_text:
            final_stdout = assistant_text
        else:
            final_stdout = out_drain.text()
        stderr_text = err_drain.text()

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
                output=final_stdout,
                stderr=stderr_text,
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

        # Subscription quota exhausted: a distinct, typed failure (plus an
        # on-disk marker) so the engine defers the job instead of failing it.
        _raise_if_usage_limit(proc.returncode, final_stdout, stderr_text)

        return ClaudeResult(
            returncode=proc.returncode,
            stdout=final_stdout,
            stderr=stderr_text,
            command=cmd,
            start_ts=start_ts,
            end_ts=end_ts,
        )


def printable_command(command: list[str]) -> list[str]:
    return _redact_command(command)
