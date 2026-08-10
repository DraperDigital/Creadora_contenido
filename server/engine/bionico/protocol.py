"""Local URL protocol launcher for the dashboard restart button.

This is deliberately NOT login autostart. It registers a per-user Windows URL
scheme so a browser click can launch `bionico protocol-open ...` on this PC even
when the agent/watcher are not running.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SCHEME = "bionico-v5-2"
URL_PREFIX = SCHEME + "://"


def launcher_path() -> str:
    exe_name = "bionico.exe" if sys.platform == "win32" else "bionico"
    exe = Path(sys.executable).parent / exe_name
    if exe.exists():
        return str(exe)
    found = shutil.which("bionico")
    return found or str(exe)


def command_for(bionico_exe: str) -> str:
    return f'"{bionico_exe}" protocol-open "%1"'


def _windows_key_path() -> str:
    return r"Software\Classes\%s" % SCHEME


def _delete_key_tree(winreg, root, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_key_tree(winreg, root, path + "\\" + child)
    except FileNotFoundError:
        return
    winreg.DeleteKey(root, path)


def install(bionico_exe: str | None = None) -> bool:
    if sys.platform != "win32":
        print("bionico: protocol launcher is only implemented on Windows")
        return False
    import winreg

    exe = bionico_exe or launcher_path()
    base = _windows_key_path()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Contenido Bionico V5-2")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command_for(exe))
    print("bionico: installed %s:// launcher -> %s" % (SCHEME, exe))
    return True


def disable() -> None:
    if sys.platform != "win32":
        return
    import winreg

    _delete_key_tree(winreg, winreg.HKEY_CURRENT_USER, _windows_key_path())
    print("bionico: disabled %s:// launcher" % SCHEME)


def status() -> bool:
    if sys.platform != "win32":
        print("bionico: protocol launcher unsupported on this OS")
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _windows_key_path() + r"\shell\open\command"
        ) as key:
            cmd = winreg.QueryValueEx(key, None)[0]
    except FileNotFoundError:
        print("bionico: %s:// launcher not installed" % SCHEME)
        return False
    print("bionico: %s:// launcher installed: %s" % (SCHEME, cmd))
    return True


def open_uri(uri: str | None = None) -> int:
    if uri and not uri.lower().startswith(URL_PREFIX):
        print("bionico: ignoring unsupported protocol URL")
        return 2

    from bionico import config, orchestrator
    from bionico.agent.cloudcfg import load_cloud
    from bionico.agent.client import CloudClient

    cfg = config.load()
    cloud = load_cloud(cfg.repo_root)
    if cloud is not None:
        try:
            # The dashboard already set restart_requested_at. Ack before we
            # restart locally so the newly started agent does not restart again.
            CloudClient(cloud).ack_restart()
        except Exception:
            pass
    orchestrator.stop(cfg)
    orchestrator.start_detached(cfg)
    return 0
