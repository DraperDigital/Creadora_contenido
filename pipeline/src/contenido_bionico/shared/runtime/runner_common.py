"""Shared subprocess plumbing for the agent CLI runners (Claude and Codex).

Both runners launch a CLI, feed it stdin, drain stdout/stderr on side
threads, tee everything to sidecar logs, watch for timeouts and emit
heartbeats, and clean up descendant processes afterwards. This module owns
those mechanics so `claude_runner` and `agent_runner` only keep the
provider-specific parts (flag building and event parsing).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from contenido_bionico.shared.ffmpeg import NO_WINDOW

# In-memory tail kept per drained pipe. The full stream is already teed to
# the sidecar logs; the in-memory copy is only a last-resort fallback for the
# final message (stdout) or a diagnostic tail (stderr), so a bounded tail is
# enough and a chatty multi-hour run can't balloon the orchestrator's RSS.
STREAM_TAIL_CAP_BYTES = 4 * 1024 * 1024
_TRUNCATION_MARKER = "[...salida truncada: solo se conserva el final del stream...]\n"


@dataclass
class HeartbeatInfo:
    """Snapshot of subprocess liveness, passed to the heartbeat callback.

    `seconds_since_last_event` measures the time since the last PARSED
    stream event once events have started flowing; before the first event it
    falls back to raw stdout activity, and it is None until any stdout
    arrives. stderr output (debug noise) never counts as liveness. `stalled`
    is True when that signal has been quiet for at least
    `stall_threshold_seconds`; lets the caller distinguish "slow but alive"
    from "probably hung". `last_event_type` is the `type` field of the most
    recent stream event (e.g. `system`, `assistant`, `result`).
    `last_status_detail` is set when Claude emits a `post_turn_summary` with
    a human-readable description of what it just did — the most useful
    progress signal we get from Claude Code's CLI today.
    """
    elapsed_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    seconds_since_last_event: float | None
    stalled: bool
    event_count: int
    last_event_type: str | None
    last_status_detail: str | None


@dataclass
class ProgressState:
    """Liveness counters shared between drain threads and the heartbeat.

    `last_stdout_ts` tracks raw stdout bytes (pre-init fallback only);
    `last_event_ts` tracks parsed stream events (the real liveness signal).
    stderr deliberately has no timestamp: debug noise must not look alive.
    """
    lock: threading.Lock = field(default_factory=threading.Lock)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    last_stdout_ts: float | None = None
    last_event_ts: float | None = None
    event_count: int = 0
    last_event_type: str | None = None
    last_status_detail: str | None = None


def open_log_files(*paths) -> list:
    """Open every path for append; all-or-nothing, best-effort.

    Returns one file object (or None) per path. If ANY open fails, every
    already-opened handle is closed and all slots come back None: sidecar
    logging must never block or break the run itself.
    """
    fps: list = [None] * len(paths)
    try:
        for i, path in enumerate(paths):
            if path is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            fps[i] = path.open("a", encoding="utf-8")
        return fps
    except OSError:
        for fp in fps:
            try:
                if fp is not None:
                    fp.close()
            except OSError:
                pass
        return [None] * len(paths)


def close_quietly(*handles) -> None:
    """Close every non-None handle, swallowing errors."""
    for handle in handles:
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass


def start_process(
    cmd: list[str],
    *,
    cwd,
    env: dict[str, str] | None = None,
    stdin: int | None = subprocess.PIPE,
) -> subprocess.Popen:
    """Launch an agent CLI with the runners' standard pipe/text settings.

    On POSIX the child gets its own session (`start_new_session=True`) so it
    becomes its own process-group leader: one `killpg` later sweeps the whole
    descendant tree (backgrounded subshells included). On Windows NO_WINDOW
    keeps the console hidden.
    """
    kwargs: dict = dict(
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if env is not None:
        kwargs["env"] = env
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def send_stdin_async(proc: subprocess.Popen, text: str) -> threading.Thread:
    """Write `text` to proc.stdin and close it, on a daemon side-thread.

    A broken pipe (the CLI exiting before we finish writing) can then never
    interrupt the main flow.
    """
    def _send() -> None:
        try:
            if text and proc.stdin is not None:
                proc.stdin.write(text)
        except (BrokenPipeError, OSError):
            pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread


class StreamDrainer:
    """Drain one subprocess pipe on a daemon thread, keeping a bounded tail.

    - mode="lines": reads with readline() so stream-json events surface as
      soon as the CLI flushes them. Using `read(N)` would let TextIOWrapper
      buffer up to N chars before returning, delaying small early events
      (rate_limit_event ~290 B, init ~3 KB) by minutes on a quiet pipe.
    - mode="chunks": fixed-size reads for unstructured streams (stderr).

    Every piece is teed to `sink_fp` (best-effort) and passed to `on_data`
    (which owns byte accounting / event parsing). In memory only the last
    `tail_cap_bytes` are kept — the collected text is a fallback, never the
    primary output. Not thread-safe beyond its own single drain thread:
    call `text()` only after `join()`.
    """

    def __init__(
        self,
        stream,
        *,
        mode: str,
        sink_fp=None,
        on_data: Callable[[str], None] | None = None,
        tail_cap_bytes: int = STREAM_TAIL_CAP_BYTES,
    ) -> None:
        if mode not in {"lines", "chunks"}:
            raise ValueError(f"unknown drain mode: {mode}")
        self._stream = stream
        self._mode = mode
        self._sink_fp = sink_fp
        self._on_data = on_data
        self._cap = tail_cap_bytes
        self._chunks: deque[str] = deque()
        self._kept_bytes = 0
        self._truncated = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    def text(self) -> str:
        body = "".join(self._chunks)
        if self._truncated:
            return _TRUNCATION_MARKER + body
        return body

    def _push(self, piece: str) -> None:
        self._chunks.append(piece)
        self._kept_bytes += len(piece.encode("utf-8", errors="replace"))
        while self._kept_bytes > self._cap and len(self._chunks) > 1:
            dropped = self._chunks.popleft()
            self._kept_bytes -= len(dropped.encode("utf-8", errors="replace"))
            self._truncated = True
        if self._sink_fp is not None:
            try:
                self._sink_fp.write(piece)
                self._sink_fp.flush()
            except OSError:
                pass
        if self._on_data is not None:
            self._on_data(piece)

    def _run(self) -> None:
        try:
            if self._mode == "lines":
                for line in iter(self._stream.readline, ""):
                    if not line:
                        return
                    self._push(line)
            else:
                while True:
                    chunk = self._stream.read(8192)
                    if not chunk:
                        return
                    self._push(chunk)
        except (OSError, ValueError):
            return


def parse_stream_event(line: str) -> dict | None:
    """Parse one JSONL stream line into an event dict, or None for noise."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def start_heartbeat(
    *,
    progress: ProgressState,
    start_monotonic: float,
    interval_seconds: float | None,
    callback: Callable[[HeartbeatInfo], None] | None,
    stall_threshold_seconds: float,
) -> tuple[threading.Event, threading.Thread | None]:
    """Start the supervision heartbeat. Returns (stop_event, thread-or-None).

    Stall detection anchors on parsed stream events once any have arrived
    (the real liveness signal); before the first event it falls back to raw
    stdout activity. stderr noise never counts (see ProgressState).
    """
    stop = threading.Event()
    if callback is None or not interval_seconds:
        return stop, None

    def _loop() -> None:
        interval = max(float(interval_seconds or 0.0), 1.0)
        while not stop.wait(interval):
            now = time.monotonic()
            with progress.lock:
                stdout_bytes = progress.stdout_bytes
                stderr_bytes = progress.stderr_bytes
                last_stdout_ts = progress.last_stdout_ts
                last_event_ts = progress.last_event_ts
                event_count = progress.event_count
                last_event_type = progress.last_event_type
                last_status_detail = progress.last_status_detail
            if last_event_ts is not None:
                since_last_event = now - last_event_ts
            elif last_stdout_ts is not None:
                since_last_event = now - last_stdout_ts
            else:
                since_last_event = None
            stalled = (
                since_last_event is not None
                and since_last_event >= stall_threshold_seconds
            )
            info = HeartbeatInfo(
                elapsed_seconds=now - start_monotonic,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                seconds_since_last_event=since_last_event,
                stalled=stalled,
                event_count=event_count,
                last_event_type=last_event_type,
                last_status_detail=last_status_detail,
            )
            try:
                callback(info)
            except Exception:
                # Heartbeat is observability; never let it crash the call.
                pass

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop, thread


def _snapshot_processes() -> list[dict]:
    """Snapshot running processes via Get-CimInstance (Windows). [] on failure."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,Name | "
                "ConvertTo-Json -Compress",
            ],
            timeout=15,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )
    except Exception:
        return []
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows: list[dict] = []
    for row in data:
        try:
            rows.append(
                {
                    "pid": int(row.get("ProcessId") or 0),
                    "ppid": int(row.get("ParentProcessId") or 0),
                    "name": (row.get("Name") or "").lower(),
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def _kill_descendants_windows(parent_pid: int) -> None:
    """Force-kill every descendant of `parent_pid`. Best-effort, never raises.

    Backstop for Windows pipe leaks: a `subprocess.run`-style flow waits not
    only for the child to exit but also for its stdout/stderr pipes to EOF.
    On Windows, an agent's `run_in_background: true` Bash tool call leaves a
    bash subshell alive that can inherit those pipe handles -- which then
    keeps the pipe open after `claude.exe` itself has already exited, hanging
    Python's reader threads forever. Walking the snapshotted process tree
    and killing every descendant releases the handles.
    """
    rows = _snapshot_processes()
    if not rows:
        return
    children_by_parent: dict[int, list[int]] = {}
    for r in rows:
        children_by_parent.setdefault(r["ppid"], []).append(r["pid"])
    descendants: list[int] = []
    queue: list[int] = list(children_by_parent.get(parent_pid, []))
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        queue.extend(children_by_parent.get(pid, []))
    # Kill leaves first so we don't re-orphan deeper descendants we haven't
    # reached yet. taskkill is best-effort and silently ignores already-dead
    # PIDs.
    for pid in reversed(descendants):
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                timeout=10,
                capture_output=True,
                creationflags=NO_WINDOW,
            )
        except Exception:
            pass


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Force-kill `proc` and every descendant. Best-effort, never raises.

    POSIX: `start_process` launched the child with start_new_session=True,
    so it is its own process-group leader (pgid == pid) and one SIGKILL to
    the group sweeps the whole tree — even after the leader already exited,
    surviving group members keep the pgid alive. Windows: walk and taskkill
    the snapshotted descendant tree (see _kill_descendants_windows).
    """
    if os.name == "nt":
        _kill_descendants_windows(proc.pid)
    else:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            # Leader already reaped: with start_new_session the group id
            # still equals the child's pid while any member survives.
            pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except Exception:
        pass


def wait_or_kill_tree(proc: subprocess.Popen, timeout_seconds: int | None) -> bool:
    """Wait for `proc`; on timeout hard-kill its whole tree. True if timed out."""
    try:
        proc.wait(timeout=timeout_seconds)
        return False
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return True
