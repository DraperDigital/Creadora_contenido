"""Register/unregister 'bionico start' to run at login, per OS.

`enable` never raises: registration failures happen on real machines
(locked-down Task Scheduler policies, schtasks child processes with a broken
identity, non-systemd distros), so each failure is printed together with the
failing command's captured output, a fallback mechanism is tried where one
exists, and callers get the name of the mechanism that ended up active (or
None) instead of an exception.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TASK_NAME = "bionico"
PLIST_LABEL = "com.bionico"
STARTUP_SCRIPT_NAME = "bionico-start.vbs"


def launcher_path() -> str:
    """Absolute path of this venv's `bionico` launcher.

    Resolved from sys.executable so autostart entries and the installed
    skills never depend on the user's PATH. Falls back to a PATH lookup for
    unusual layouts (e.g. a system interpreter without the console script).
    """
    exe_name = "bionico.exe" if sys.platform == "win32" else "bionico"
    exe = Path(sys.executable).parent / exe_name
    if exe.exists():
        return str(exe)
    found = shutil.which("bionico")
    if found:
        return found
    # Degenerate last resort (no console script next to this interpreter and
    # none on PATH): still return the absolute venv-style path callers expect,
    # but say so -- skills/autostart entries registered against it will not
    # work until the launcher exists.
    print("bionico: WARN launcher not found (%s); skills/autostart entries "
          "will not work until it exists" % exe)
    return str(exe)


def windows_enable_cmd(bionico_exe: str):
    return ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME,
            "/TR", '"%s" start' % bionico_exe, "/RL", "LIMITED", "/F"]


def windows_disable_cmd():
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def windows_startup_script() -> Path:
    """Startup-folder launcher: the primary Windows autostart mechanism."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Startup" / STARTUP_SCRIPT_NAME)


def windows_startup_vbs(bionico_exe: str) -> str:
    return ("' Start Contenido Bionico V5 at logon, hidden (no console window).\n"
            "Dim sh\n"
            'Set sh = CreateObject("WScript.Shell")\n'
            'sh.Run """%s"" start", 0, False\n' % bionico_exe)


def macos_plist(bionico_exe: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>Label</key><string>%s</string>\n'
            '  <key>ProgramArguments</key><array>\n'
            '    <string>%s</string><string>start</string>\n'
            '  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><false/>\n'
            '</dict></plist>\n' % (PLIST_LABEL, bionico_exe))


def linux_unit(bionico_exe: str) -> str:
    return ("[Unit]\nDescription=Contenido Bionico V5\nAfter=network.target\n\n"
            "[Service]\nType=simple\nExecStart=%s start\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n" % bionico_exe)


def _run(cmd):
    """Run a command without ever raising; returns (returncode, output text)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except OSError as exc:  # the tool itself is missing or unrunnable
        return 1, str(exc)
    out = "\n".join(s.strip() for s in (proc.stderr, proc.stdout) if s and s.strip())
    return proc.returncode, out


def _print_output(out: str) -> None:
    if out:
        print("  " + out.replace("\n", "\n  "))


def _windows_enable(bionico_exe: str) -> str | None:
    # Startup folder first: it launches in the interactive Explorer session,
    # the same context as starting by hand. A schtasks ONLOGON task spawned
    # Syncthing with a broken identity on a real install (the phone
    # un-paired after every reboot), so Task Scheduler is only the fallback
    # when the Startup folder is unwritable.
    script = windows_startup_script()
    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        # UTF-16 (with BOM): WSH reads ANSI or Unicode scripts, and Unicode
        # keeps non-ASCII launcher paths (user names) intact.
        script.write_text(windows_startup_vbs(bionico_exe), encoding="utf-16")
    except OSError as exc:
        print("Could not write %s: %s" % (script, exc))
        print("Falling back to a Task Scheduler logon task...")
        code, out = _run(windows_enable_cmd(bionico_exe))
        if code == 0:
            print("Autostart enabled via Task Scheduler (task '%s')." % TASK_NAME)
            return "Task Scheduler"
        print("Task Scheduler registration failed (schtasks exit %s):" % code)
        _print_output(out)
        return None
    # Drop any scheduled task from an earlier install so a login does not
    # start two instances.
    _run(windows_disable_cmd())
    print("Autostart enabled via Startup folder (%s)." % script)
    return "Startup folder"


def _macos_enable(bionico_exe: str) -> str | None:
    p = Path.home() / "Library" / "LaunchAgents" / (PLIST_LABEL + ".plist")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(macos_plist(bionico_exe), encoding="utf-8")
    except OSError as exc:
        print("Could not write %s: %s" % (p, exc))
        return None
    # Output is ignored on purpose: re-loading an already-loaded agent
    # complains, and launchd picks the plist up at next login regardless.
    _run(["launchctl", "load", str(p)])
    print("Autostart enabled via launchd (%s)." % p)
    return "launchd"


def _linux_enable(bionico_exe: str) -> str | None:
    p = Path.home() / ".config" / "systemd" / "user" / "bionico.service"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(linux_unit(bionico_exe), encoding="utf-8")
    except OSError as exc:
        print("Could not write %s: %s" % (p, exc))
        return None
    code, out = _run(["systemctl", "--user", "enable", "--now", "bionico.service"])
    if code != 0:
        print("systemctl --user enable failed (exit %s):" % code)
        _print_output(out)
        return None
    print("Autostart enabled via systemd user service (%s)." % p)
    return "systemd"


def enable(bionico_exe: str) -> str | None:
    """Register 'bionico start' at login. Never raises.

    Prints what happened -- including a failing command's captured
    stderr/stdout so the cause is visible -- and returns the name of the
    mechanism that ended up active ("Task Scheduler", "Startup folder",
    "launchd", "systemd"), or None when nothing could be registered.
    """
    if sys.platform == "win32":
        return _windows_enable(bionico_exe)
    if sys.platform == "darwin":
        return _macos_enable(bionico_exe)
    return _linux_enable(bionico_exe)


def status_mechanism() -> str | None:
    """Which login-autostart mechanism is currently registered, or None.

    Best-effort and read-only; mirrors what enable() may have installed
    (on Windows both the Startup-folder launcher and the scheduled task)."""
    if sys.platform == "win32":
        try:
            if windows_startup_script().exists():
                return "Startup folder"
        except OSError:
            pass
        code, _ = _run(["schtasks", "/Query", "/TN", TASK_NAME])
        return "Task Scheduler" if code == 0 else None
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "LaunchAgents" / (PLIST_LABEL + ".plist")
        return "launchd" if p.exists() else None
    code, _ = _run(["systemctl", "--user", "is-enabled", "bionico.service"])
    return "systemd" if code == 0 else None


def disable() -> None:
    """Unregister login autostart. Removes every mechanism enable() may have
    installed (on Windows: both the scheduled task and the Startup-folder
    launcher). Never raises."""
    if sys.platform == "win32":
        _run(windows_disable_cmd())
        try:
            windows_startup_script().unlink(missing_ok=True)
        except OSError:
            pass
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "LaunchAgents" / (PLIST_LABEL + ".plist")
        _run(["launchctl", "unload", str(p)])
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        _run(["systemctl", "--user", "disable", "--now", "bionico.service"])
        try:
            (Path.home() / ".config" / "systemd" / "user" / "bionico.service").unlink(missing_ok=True)
        except OSError:
            pass
