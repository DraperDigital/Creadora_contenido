"""Install the AI-client skills V5 ships into Claude Code / Codex.

- /bionico       -> start/restart the local engine (this package)
- /short         -> manual, non-automated video editing via the vendored
                    contenido-bionico pipeline

The /bionico skill is authored here; /short is delegated to the pipeline's own
installer so it stays in sync with the pipeline version.

Install location follows the pipeline's convention and honors
BIONICO_SKILL_HOME (used by tests): Claude commands at
<home>/.claude/commands/<name>.md, Codex skills at
<home>/.codex/skills/<name>/SKILL.md.

Skill bodies invoke the venv launcher by ABSOLUTE path (resolved at install
time from this interpreter's directory), so they work in any shell without the
user ever adding the venv to PATH.
"""
from __future__ import annotations

import os
from pathlib import Path

from bionico import autostart

_BIONICO_DESC = (
    "Start or restart the Contenido Bionico V5 local engine "
    "(watcher, plus the cloud agent when configured)."
)


def _bionico_body(exe: str) -> str:
    return """# /bionico

Use this when the user types `/bionico`: start the Contenido Bionico V5 local
engine (or restart it if already running).

This controls only the supervised daemons: the LITE watcher and, when the cloud
is configured, the pull agent. It does NOT touch the video-editing pipeline: any
edit already in progress keeps running, untouched, through the restart.

Steps:
1. Run this shell command: `"%(exe)s" restart --detach`
   It (re)starts the watcher (and the cloud agent when configured) in the
   background and returns immediately. If nothing was running, it just starts
   them.
2. Report what it prints. Confirm the services with `"%(exe)s" status`.
3. If that launcher does not exist, tell the user to run the repo's install
   script first (`install.ps1` on Windows, `install.sh` on macOS/Linux).

Notes:
- Stop the daemons with `"%(exe)s" stop` (in-progress edits keep running);
  check services with `"%(exe)s" status`.
""" % {"exe": exe}


def _authored(exe: str):
    """Authored skills shipped by THIS package: (name, description, body)."""
    return (
        ("bionico", _BIONICO_DESC, _bionico_body(exe)),
    )


def _home() -> Path:
    override = os.environ.get("BIONICO_SKILL_HOME")
    return Path(override).expanduser() if override else Path.home()


def _skill_path(target: str, name: str) -> Path:
    if target == "codex":
        return _home() / ".codex" / "skills" / name / "SKILL.md"
    return _home() / ".claude" / "commands" / ("%s.md" % name)


def _skill_text(target: str, name: str, desc: str, body: str) -> str:
    if target == "codex":
        return "---\nname: %s\ndescription: %s\n---\n\n%s" % (name, desc, body)
    return body


def detect_targets() -> list[str]:
    """AI clients to install into. Prefer the pipeline's detector; fall back to
    a home-dir probe so /bionico installs even if the pipeline import fails."""
    try:
        from contenido_bionico.skill_installer import detected_targets
        targets = detected_targets()
        if targets:
            return targets
    except Exception:  # noqa: BLE001 - fall through to the local probe
        pass
    home = _home()
    out = []
    if (home / ".codex").exists():
        out.append("codex")
    if (home / ".claude").exists():
        out.append("claude")
    return out or ["claude"]


def install_bionico_command(targets) -> list[str]:
    """Install every authored skill (/bionico) for each target.

    Bodies embed the absolute launcher path so the skills never depend on
    `bionico` being on the user's PATH.
    Returns the list of targets that received the skills.
    """
    exe = autostart.launcher_path()
    done = []
    for t in targets:
        for name, desc, body in _authored(exe):
            path = _skill_path(t, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_skill_text(t, name, desc, body).rstrip() + "\n", encoding="utf-8")
        done.append(t)
    return done


def install_pipeline_skills() -> list[str]:
    from contenido_bionico.skill_installer import install_bionico_skill
    return install_bionico_skill("auto")


def install_all(cfg=None) -> None:
    targets = detect_targets()
    print("--- installing AI-client skills ---")
    try:
        from contenido_bionico.skill_installer import skill_command_list
        names = skill_command_list()
    except Exception:  # noqa: BLE001 - fall back to a generic label
        names = "pipeline"
    try:
        pipe = install_pipeline_skills()
        print("[OK] %s installed for: %s" % (names, ", ".join(pipe) or "none"))
    except Exception as exc:  # noqa: BLE001 - skills are best-effort
        print("[!!] could not install %s: %s" % (names, exc))
    try:
        done = install_bionico_command(targets)
        print("[OK] /bionico installed for: %s" % (", ".join(done) or "none"))
    except Exception as exc:  # noqa: BLE001
        print("[!!] could not install /bionico: %s" % exc)
