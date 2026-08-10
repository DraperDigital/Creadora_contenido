"""Supervise the Bionico services as child processes."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

MAX_BACKOFF = 60


def backoff(failures: int) -> int:
    return min(MAX_BACKOFF, 2 ** failures)


def child_specs(cfg):
    """Children run as `python -m ...`; the agent is added only when cloud is configured."""
    py = sys.executable
    specs = [
        {"name": "watcher", "cmd": [py, "-m", "bionico.watcher.watcher"]},
    ]
    from bionico import localdash
    if localdash.available(cfg.repo_root):
        localdash.bootstrap(cfg.repo_root)  # writes server/cloud/.env before gating
        specs.append(localdash.spec(cfg.repo_root))
    from bionico.agent.cloudcfg import load_cloud
    if load_cloud(cfg.repo_root) is not None:
        specs.append({"name": "agent", "cmd": [py, "-m", "bionico.agent.agent"]})
    return specs


def _pidfile(cfg) -> Path:
    return cfg.work_dir / "supervisor.json"


def _spawn(spec, cfg):
    return subprocess.Popen(spec["cmd"], cwd=str(cfg.repo_root))


def _excluded_pids():
    """This process plus its whole ancestor chain — never reap these.

    Walking ancestors keeps the reaper from killing the very command that
    launched it (e.g. `bionico start --detach`, which matches the supervisor
    signature) when it runs inside a freshly-spawned detached supervisor.
    """
    pids = set()
    try:
        proc = psutil.Process()
        while proc is not None:
            pids.add(proc.pid)
            proc = proc.parent()
    except psutil.Error:
        pass
    return pids


def _repo_scoped(cmd, cfg):
    """True when any argv token resolves under this repo's root.

    Every daemon this supervisor spawns carries a repo-rooted path in argv
    (the venv python, the venv bionico launcher, or server/local/server.mjs),
    so scoping by argv path keeps the reaper away from OTHER bionico
    installs running on the same machine.
    """
    root = str(cfg.repo_root).replace("\\", "/").rstrip("/").lower() + "/"
    for c in cmd:
        if str(c).replace("\\", "/").lower().startswith(root):
            return True
    return False


def _classify_orphan(cmd, cfg):
    """Label a process as a reapable Bionico daemon, or return None.

    Matches ONLY this repo's watcher, agent, dashboard, and supervisor. Uses
    exact argv tokens for the module daemons so detached edit jobs
    (`bionico.watcher.jobrunner` and their ffmpeg/node children) are never
    matched.
    """
    if not cmd:
        return None
    if not _repo_scoped(cmd, cfg):
        return None
    if "bionico.watcher.watcher" in cmd:
        return "watcher"
    if "bionico.agent.agent" in cmd:
        return "agent"
    if any(str(c).replace("\\", "/").endswith("server/local/server.mjs") for c in cmd):
        return "dashboard"
    if "start" in cmd and (
        "bionico.cli" in cmd
        or any(Path(c).name in ("bionico", "bionico.exe") for c in cmd)
    ):
        return "supervisor"
    return None


def _reap_orphans(cfg):
    """Kill leftover Bionico daemons from earlier runs before starting fresh.

    `stop()` only kills PIDs in the pidfile, so a daemon from a crashed or
    parallel supervisor survives and blocks the new instance. This sweeps the
    whole process table by command line and removes every match.

    In-progress video edits are preserved: edit jobs run as
    `bionico.watcher.jobrunner` and are never matched, and we kill exact PIDs
    only (no tree-kill), so a reaped watcher leaves its detached jobs running.
    """
    skip = _excluded_pids()
    found = []  # (label, pid)
    for proc in psutil.process_iter(["pid", "cmdline"]):
        pid = proc.info.get("pid")
        if pid in skip:
            continue
        try:
            label = _classify_orphan(proc.info.get("cmdline") or [], cfg)
        except psutil.Error:
            continue
        if label:
            found.append((label, pid))
    # Kill supervisors first so they cannot respawn the children we kill next.
    found.sort(key=lambda lp: 0 if lp[0] == "supervisor" else 1)
    for label, pid in found:
        _kill_pid(pid)
        print("bionico: reaped orphan %s (pid %s)" % (label, pid))
    if found:
        time.sleep(1)  # let the OS release locks before we respawn
    return found


def start(cfg):
    cfg.ensure_dirs()
    _reap_orphans(cfg)
    specs = child_specs(cfg)
    procs = {}
    for s in specs:
        p = _spawn(s, cfg)
        if p is not None:
            procs[s["name"]] = p
        else:
            print("bionico: %s not available, skipping (run 'bionico install')" % s["name"])
    _write_pidfile(cfg, procs)
    print("bionico: started %s" % ", ".join(sorted(procs)))
    sys.stdout.flush()
    # Only supervise services that actually started; a None spawn is skipped
    # instead of being treated as a perpetual crash.
    supervised = [s for s in specs if s["name"] in procs]
    failures = {s["name"]: 0 for s in supervised}
    try:
        while True:
            for s in supervised:
                name = s["name"]
                p = procs.get(name)
                if p is not None and p.poll() is None:
                    continue
                failures[name] += 1
                wait = backoff(failures[name])
                print("bionico: %s died, restarting in %ds" % (name, wait))
                time.sleep(wait)
                np = _spawn(s, cfg)
                if np is not None:
                    procs[name] = np
                    _write_pidfile(cfg, procs)
            time.sleep(2)
    except KeyboardInterrupt:
        stop(cfg)


def _detached_popen(cmd, cfg):
    """Spawn a fully detached supervisor that outlives the launching process."""
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    log = open(cfg.work_dir / "supervisor.log", "ab")
    kwargs = dict(cwd=str(cfg.repo_root), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: a console-less parent makes
        # every console child (server/watcher) allocate its own VISIBLE console
        # window. A hidden console is inherited by the children instead.
        create_no_window = 0x08000000
        create_new_process_group = 0x00000200
        kwargs["creationflags"] = create_no_window | create_new_process_group
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def start_detached(cfg):
    """Start the supervised stack in the background and return immediately.

    Used by `bionico start --detach` (and the /bionico skill) so a session does
    not block on the foreground supervise loop. The child writes the pidfile.
    """
    cfg.ensure_dirs()
    exe = shutil.which("bionico")
    cmd = [exe, "start"] if exe else [sys.executable, "-m", "bionico.cli", "start"]
    _detached_popen(cmd, cfg)
    for _ in range(40):  # up to ~10s for the child to write the pidfile
        time.sleep(0.25)
        if _read_pidfile(cfg):
            break
    print("bionico: started in background.")
    status(cfg)


def _write_pidfile(cfg, procs):
    data = {"supervisor": psutil.Process().pid,
            "children": {n: p.pid for n, p in procs.items()}}
    _pidfile(cfg).parent.mkdir(parents=True, exist_ok=True)
    _pidfile(cfg).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_pidfile(cfg):
    try:
        return json.loads(_pidfile(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _kill_pid(pid):
    """Kill ONLY this process, never its children.

    The watcher's children are detached video-editing jobs; a tree-kill here
    would interrupt an edit in progress. Stopping the stack must leave those
    running, so we target exact daemon PIDs only.
    """
    try:
        psutil.Process(pid).kill()
    except psutil.NoSuchProcess:
        pass


def stop(cfg):
    data = _read_pidfile(cfg)
    if not data:
        print("bionico: not running (no pidfile)")
        return
    # Kill the supervisor first so it stops respawning children mid-shutdown.
    sup = data.get("supervisor")
    if sup and sup != psutil.Process().pid:
        _kill_pid(sup)
    for name, pid in data.get("children", {}).items():
        _kill_pid(pid)
        print("bionico: stopped %s" % name)
    _pidfile(cfg).unlink(missing_ok=True)


def status(cfg):
    from bionico import autostart
    data = _read_pidfile(cfg)
    if not data:
        print("bionico: stopped")
    else:
        children = data.get("children", {})
        for name, pid in children.items():
            alive = psutil.pid_exists(pid)
            print("  %-10s %s (pid %s)" % (name, "running" if alive else "dead", pid))
    from bionico import localdash
    if localdash.available(cfg.repo_root):
        dash_pid = data.get("children", {}).get("dashboard") if data else None
        if dash_pid is not None and not psutil.pid_exists(dash_pid):
            print("  %-10s %s" % ("dashboard",
                                  "dead — check server/engine/runtime/_work/supervisor.log (Node >= 22.5 required)"))
        else:
            for u in localdash.urls(cfg.repo_root):
                print("  %-10s %s" % ("dashboard", u))
    mech = autostart.status_mechanism()
    print("  %-10s %s" % ("autostart",
                          ("registered via %s" % mech) if mech else "not registered"))
