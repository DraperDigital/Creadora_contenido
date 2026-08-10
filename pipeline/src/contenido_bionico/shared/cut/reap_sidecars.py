"""Reap orphaned Claude Code Bash/PowerShell task processes on Windows.

Background
----------
When an agent in Claude Code runs a Bash tool call on Windows, Claude Code
spawns helper processes that read or tail the underlying command's stdout
file so the agent can read incremental output via BashOutput. Two leak
patterns are known on Windows:

  1. PowerShell sidecar — `powershell.exe Get-Content -Wait -Tail 50` tailing
     a file under `%LOCALAPPDATA%\\Temp\\claude\\<project>\\<session>\\tasks\\`.
     Claude Code's cleanup of these is racy and sometimes leaks one.

  2. Background bash subshell — `bash.exe -c "... tail -f ...tasks/<id>.output
     | grep ..."` left running because the agent never invoked `KillShell`
     on a `run_in_background: true` Bash tool call (or a foreground tail-and-grep
     never terminated).

Either pattern, leaked, prevents the parent `claude.exe` from exiting cleanly
(Windows has no SIGHUP / process group), which blocks any external watcher
waiting on `claude.exe` to return.

What this helper does
---------------------
Finds the current process's `claude.exe` ancestor, enumerates every leaked
PowerShell sidecar AND every leaked bash task subshell whose ancestor chain
leads back to that same `claude.exe`, and force-terminates them (with /T so
their own subshells go too). Skips any process that is in OUR own ancestor
chain — we must not kill the bash that is currently running this helper.

Always safe to call: no-op on non-Windows, never raises, never blocks longer
than a few seconds. Intended to be the very last action an orchestrating
agent takes before reporting completion.

Usage
-----
    python -m contenido_bionico.shared.cut.reap_sidecars
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# This file is also run by path as a child process (see shared/cut/orchestrator),
# so make the package importable before the package import below.
_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# CREATE_NO_WINDOW: don't flash a console window on Windows when shelling out.
from contenido_bionico.shared.ffmpeg import NO_WINDOW as _NO_WIN  # noqa: E402


def _list_processes() -> list[dict]:
    """Return [{pid, ppid, name, cmd}, ...] for every running process via CIM."""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress",
    ]
    try:
        out = subprocess.check_output(
            cmd,
            timeout=15,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WIN,
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
                    "cmd": row.get("CommandLine") or "",
                }
            )
        except (TypeError, ValueError):
            continue
    return rows


def _walk_ancestors(start_pid: int, by_pid: dict[int, dict]) -> list[int]:
    """Yield the ancestor chain [start_pid, parent, grandparent, ...]."""
    chain: list[int] = []
    seen: set[int] = set()
    cur = start_pid
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        node = by_pid.get(cur)
        if not node:
            break
        cur = node["ppid"]
    return chain


def _find_claude_ancestor(start_pid: int, by_pid: dict[int, dict]) -> int | None:
    """Return the PID of the nearest `claude.exe` ancestor, or None."""
    for pid in _walk_ancestors(start_pid, by_pid):
        node = by_pid.get(pid)
        if node and node["name"] == "claude.exe":
            return pid
    return None


def _is_leaked_task(name: str, cmd: str) -> bool:
    """True if this process looks like a leaked Claude Code Bash-tool task.

    Two known patterns, both required to reference a file under
    `%LOCALAPPDATA%\\Temp\\claude\\<project>\\<session>\\tasks\\` (matched
    case-insensitively against both `\\` and `/` separators):

      * PowerShell: `powershell ... Get-Content ... -Wait ...` — the legacy
        BashOutput streaming sidecar.
      * Bash: any `bash.exe ...` whose command line points at a tasks/
        output file — typically a background `tail -f ... | grep ...` left
        alive after the agent forgot to KillShell, or a foreground tool call
        that never terminated.

    The temp-path requirement is what makes this safe to apply broadly: no
    normal Windows process touches that exact path, so false positives
    against legitimate user shells are not a realistic concern.
    """
    if not cmd:
        return False
    low = cmd.lower()
    # Normalize separators so the pattern matches whether the command line
    # was built with Windows backslashes or POSIX forward slashes.
    norm = low.replace("\\", "/")
    if "temp/claude" not in norm or "/tasks/" not in norm:
        return False
    if name == "powershell.exe":
        return "get-content" in low and "-wait" in low
    if name == "bash.exe":
        # Any bash subshell whose command line references the tasks/ output
        # path is a Bash-tool descendant, not a user shell. Kill it.
        return True
    return False


def _kill(pid: int) -> bool:
    """taskkill /F /T a single PID. Best-effort; returns True on apparent success.

    /T (tree) ensures any subshells the leaked process itself spawned go down
    with it -- e.g. a leaked `bash -c "tail -f | grep"` has an inner subshell
    running the actual pipeline that must die together with its launcher.
    """
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            timeout=10,
            capture_output=True,
            text=True,
            creationflags=_NO_WIN,
        )
        return result.returncode == 0
    except Exception:
        return False


def reap() -> tuple[int, list[int]]:
    """Find and kill orphan sidecar PowerShells under our `claude.exe` ancestor.

    Returns (killed_count, killed_pids). Never raises.
    """
    if os.name != "nt":
        return 0, []
    try:
        rows = _list_processes()
        if not rows:
            return 0, []
        by_pid = {r["pid"]: r for r in rows}

        my_claude = _find_claude_ancestor(os.getpid(), by_pid)
        if my_claude is None:
            # Not running under a claude.exe session -- nothing to clean.
            return 0, []

        # Snapshot our own ancestor chain so we never kill the bash.exe
        # that is currently running this helper. Without this guard we'd
        # taskkill /T our own parent shell mid-execution.
        my_ancestors = set(_walk_ancestors(os.getpid(), by_pid))

        killed_pids: list[int] = []
        for row in rows:
            if row["name"] not in ("powershell.exe", "bash.exe"):
                continue
            if not _is_leaked_task(row["name"], row["cmd"]):
                continue
            # Only kill processes whose ancestor chain leads back to OUR
            # claude.exe. This protects sibling Claude Code sessions
            # (e.g. an open VS Code chat) from collateral kills.
            if _find_claude_ancestor(row["pid"], by_pid) != my_claude:
                continue
            if row["pid"] in my_ancestors:
                continue
            if _kill(row["pid"]):
                killed_pids.append(row["pid"])
        return len(killed_pids), killed_pids
    except Exception:
        return 0, []


def main() -> int:
    count, pids = reap()
    if count:
        print(f"reap_sidecars: killed {count} leaked task process(es): {pids}", flush=True)
    else:
        print("reap_sidecars: no leaked task processes found", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
