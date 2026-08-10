"""Central Remotion runtime paths and dependency checks."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from contenido_bionico.shared import config
from contenido_bionico.shared.ffmpeg import NO_WINDOW


PACKAGE_ROOT = config.REPO_ROOT
PACKAGE_JSON = PACKAGE_ROOT / "package.json"
PACKAGE_LOCK = PACKAGE_ROOT / "package-lock.json"
NODE_MODULES = PACKAGE_ROOT / "node_modules"
REMOTION_APP_DIR = Path(__file__).resolve().parent / "remotion"
REMOTION_PUBLIC = REMOTION_APP_DIR / "public"
INSTALL_TIMEOUT_SECONDS = 10 * 60
# Generous ceiling: on a fresh install the first `remotion versions` run reads
# thousands of just-written node_modules files with a cold cache while the
# antivirus scans each one -- measured at ~80s on a real machine, so 60s
# produced false FALTA verdicts on healthy installs.
CHECK_TIMEOUT_SECONDS = 300


def _platform_exe(name: str) -> str:
    return f"{name}.cmd" if os.name == "nt" else name


def npm_cmd() -> str:
    return _platform_exe("npm")


def remotion_bin() -> Path:
    return NODE_MODULES / ".bin" / _platform_exe("remotion")


def remotion_paths_summary() -> str:
    return (
        f"package={PACKAGE_JSON}; cli={remotion_bin()}; "
        f"app={REMOTION_APP_DIR}"
    )


_EXACT_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def _pinned_remotion_cli_version() -> str | None:
    """The @remotion/cli version pinned in the root package.json.

    Returns None (skip the drift check) when the file is unreadable, the
    dependency is missing, or the spec is a range (^/~/...) instead of an
    exact pin — never false-alarm on something we cannot compare."""
    try:
        data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    spec = None
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict) and isinstance(deps.get("@remotion/cli"), str):
            spec = deps["@remotion/cli"].strip()
            break
    if not spec or not _EXACT_VERSION_RE.fullmatch(spec):
        return None
    return spec


def _installed_remotion_cli_version() -> str | None:
    """The @remotion/cli version actually installed under node_modules.

    Returns None (skip the drift check) when the installed package.json is
    missing or unparseable."""
    pkg = NODE_MODULES / "@remotion" / "cli" / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        return None
    return version.strip()


def check_remotion_ready() -> tuple[bool, str]:
    if not PACKAGE_JSON.exists():
        return False, f"falta package.json raiz: {PACKAGE_JSON}"
    # No lockfile check: the repo intentionally ships without package-lock.json
    # (Remotion deps are "latest" and .npmrc sets package-lock=false), so the
    # readiness marker is the installed tree itself, not a lockfile.

    bin_path = remotion_bin()
    if not bin_path.exists():
        return False, f"falta Remotion CLI instalado: {bin_path}"

    try:
        proc = subprocess.run(
            [str(bin_path), "versions"],
            cwd=str(REMOTION_APP_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"no pude ejecutar Remotion CLI: {exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return False, f"Remotion CLI fallo: {tail}"

    # Drift check: the installed @remotion/cli must match the lockfile pin.
    # Tolerant by design — if either side cannot be parsed, stay "listo"
    # instead of false-alarming.
    pinned = _pinned_remotion_cli_version()
    installed = _installed_remotion_cli_version()
    if pinned and installed and pinned != installed:
        return False, (
            f"Remotion desactualizado (instalado {installed}, esperado {pinned}): "
            "ejecuta npm install en la raiz del repo"
        )

    return True, str(bin_path)


def ensure_remotion_dependencies() -> tuple[bool, str]:
    npm = shutil.which(npm_cmd()) or shutil.which("npm")
    if npm is None:
        return False, "npm no esta en PATH; instala Node.js y vuelve a correr setup."
    if not PACKAGE_JSON.exists():
        return False, f"no existe package.json raiz: {PACKAGE_JSON}"

    # npm install (not `npm ci`): the repo has no lockfile by design, so the
    # deps resolve to the "latest" specs in package.json at install time.
    label = "npm install"
    try:
        proc = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--loglevel=error"],
            cwd=str(PACKAGE_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"{label} paso del tiempo limite (10 min)."
    except OSError as exc:
        return False, f"no pude ejecutar npm: {exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return False, f"{label} fallo instalando las dependencias de Remotion: {tail}"

    return check_remotion_ready()
