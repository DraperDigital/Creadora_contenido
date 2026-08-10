"""The local LAN dashboard (server/local) as a supervised child process."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def server_script(repo_root: Path) -> Path:
    return Path(repo_root) / "server" / "local" / "server.mjs"


def available(repo_root: Path) -> bool:
    return server_script(repo_root).exists() and shutil.which("node") is not None


def bootstrap(repo_root: Path) -> None:
    """Generate/refresh server/local/.env and the managed engine wiring in
    server/cloud/.env BEFORE cloud gating runs, so the agent starts on the
    very first `bionico start`. Best-effort: the server re-runs it at boot."""
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        ver = tuple(int(x) for x in out.lstrip("v").split(".")[:2])
        if ver < (22, 5):
            print("bionico: WARNING — Node %s found; the local dashboard needs Node >= 22.5 "
                  "and will not start." % out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    try:
        subprocess.run(
            ["node", str(server_script(repo_root)), "--bootstrap-only"],
            cwd=str(repo_root), check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def spec(repo_root: Path) -> dict:
    return {"name": "dashboard", "cmd": ["node", str(server_script(repo_root))]}


def urls(repo_root: Path, port_default: int = 8787) -> list[str]:
    """Dashboard URLs for `bionico status` (loopback + every LAN IPv4)."""
    import psutil

    port = port_default
    env_file = Path(repo_root) / "server" / "local" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("LOCAL_DASHBOARD_PORT="):
                port = int(line.split("=", 1)[1].strip() or port_default)
    except (OSError, ValueError):
        pass
    out = ["http://127.0.0.1:%d" % port]
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            ip = getattr(a, "address", "")
            if getattr(a, "family", None) is not None and a.family.name == "AF_INET" \
                    and ip and not ip.startswith("127."):
                out.append("http://%s:%d" % (ip, port))
    return out
