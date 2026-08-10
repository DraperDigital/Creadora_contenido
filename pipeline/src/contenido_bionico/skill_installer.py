"""Install contenido-bionico slash skills into supported local AI clients."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
TARGETS = {"auto", "all", "codex", "claude"}
COMMUNITY_URL = "https://www.skool.com/bionico"
SKILL_BUNDLE_VERSION = "contenido-bionico-install-contract-v0.4.5"


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    repo_fallback: str | None = None


SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="short",
        description="video vertical 9:16: corte + animacion con captions, o solo cortar / solo animar",
    ),
)

# Artifacts written by older releases that current installs no longer ship.
# They invoke removed CLI flags, so they are deleted on (re)install. Only
# these exact names are ever removed -- never glob patterns. Also covers the
# retired standalone editions (comentario/ranking) whose flows merged into
# this pipeline's --comment/--ranking modes.
LEGACY_CLAUDE_COMMANDS: tuple[str, ...] = (
    "cortar.md",
    "animar.md",
    "animar-full.md",
    "bionico-onboarding.md",
    "bionico-pair.md",
    "comentario.md",
    "ranking.md",
    "long.md",
)
LEGACY_CLAUDE_DIRS: tuple[str, ...] = ("bionico-docs",)
# NOTE: "bionico"/"bionico.md" must NEVER appear in these legacy lists -- the
# orchestrator installs /bionico as a current skill (it starts the server) and
# a pipeline (re)install must not delete it.
LEGACY_CODEX_SKILL_DIRS: tuple[str, ...] = (
    "cortar", "animar", "animar-full",
    "bionico-pair", "contenido-bionico-lite",
    "comentario", "ranking", "long",
)


def skill_command_names() -> list[str]:
    return [f"/{skill.name}" for skill in SKILLS]


def skill_command_list() -> str:
    names = skill_command_names()
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " y " + names[-1]


def _home() -> Path:
    override = os.environ.get("BIONICO_SKILL_HOME")
    return Path(override).expanduser() if override else Path.home()


def skill_source(skill: SkillSpec) -> Path:
    packaged = PACKAGE_ROOT / skill.name / "SKILL.md"
    if packaged.exists():
        return packaged
    if skill.repo_fallback:
        return PACKAGE_ROOT.parents[1] / skill.repo_fallback
    return packaged


def read_skill_text(skill: SkillSpec) -> str:
    source = skill_source(skill)
    return source.read_text(encoding="utf-8")


def codex_skill_path(skill_name: str) -> Path:
    return _home() / ".codex" / "skills" / skill_name / "SKILL.md"


def claude_command_path(skill_name: str) -> Path:
    return _home() / ".claude" / "commands" / f"{skill_name}.md"


def _codex_help_dir() -> Path:
    return _home() / ".codex" / "skills" / "contenido-bionico" / "docs" / "help"


def _claude_help_dir() -> Path:
    return _home() / ".claude" / "commands" / "contenido-bionico-docs" / "help"


def help_docs_source() -> Path | None:
    packaged = PACKAGE_ROOT / "shared" / "help"
    if packaged.exists():
        return packaged
    return None


def detected_targets() -> list[str]:
    home = _home()
    out: list[str] = []
    if shutil.which("codex") or (home / ".codex").exists():
        out.append("codex")
    if shutil.which("claude") or (home / ".claude").exists():
        out.append("claude")
    return out


def resolve_targets(target: str) -> list[str]:
    if target not in TARGETS:
        raise ValueError(f"unknown skill install target: {target}")
    if target == "all":
        return ["codex", "claude"]
    if target in {"codex", "claude"}:
        return [target]
    detected = detected_targets()
    return detected if detected else ["codex", "claude"]


def required_skill_target_for_provider(provider: str | None) -> str | None:
    if provider == "claude_code":
        return "claude"
    if provider == "codex_cli":
        return "codex"
    return None


def target_display_name(target: str) -> str:
    return {"codex": "Codex", "claude": "Claude"}.get(target, target)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _cli_invocation() -> str:
    """Absolute path of the contenido-bionico launcher in the owning venv.

    The bare command is NOT on the system PATH (the pipeline lives inside the
    repo's venv), so installed skills must tell the assistant the real path.
    Resolved at install time from the interpreter running this installer.
    """
    exe = "contenido-bionico.exe" if os.name == "nt" else "contenido-bionico"
    return str(Path(sys.executable).parent / exe)


def _install_contract_text(*, help_dir: Path | None = None) -> str:
    help_text = f"\n- Guias de soporte instaladas: `{help_dir}`." if help_dir else ""
    return "\n".join(
        [
            f"<!-- {SKILL_BUNDLE_VERSION} -->",
            "# Contrato obligatorio de instalacion",
            "",
            (
                "- IMPORTANTE: el comando `contenido-bionico` NO esta en el PATH del sistema. "
                f'Cada vez que esta guia diga `contenido-bionico ...`, ejecuta `"{_cli_invocation()}" ...` '
                "(la misma orden con esa ruta absoluta)."
            ),
            (
                "Si el usuario pide clonar, instalar, configurar, dejar listo, usar por primera vez "
                "o arreglar una instalacion de contenido-bionico, lee y sigue `AGENTS.md` del repo "
                "como fuente de verdad antes de improvisar."
            ),
            "",
            "- No digas que la instalacion quedo lista hasta que `contenido-bionico doctor` pase.",
            (
                "- La instalacion completa debe dejar disponibles estos comandos: "
                f"{skill_command_list()}."
            ),
            (
                "- El resumen final de instalacion en espanol es obligatorio: debe nombrar los skills "
                "instalados, explicar brevemente para que sirve cada uno y mostrar la comunidad oficial "
                f"{COMMUNITY_URL} para ayuda de implementacion, uso y personalizacion."
            ),
            "- Si falta algo, arreglalo y vuelve a correr `contenido-bionico doctor`.",
            (
                "- Durante produccion, las animaciones pueden tardar 10-20 minutos por escena. "
                "No cierres procesos, no relances y no cambies de estrategia por silencio. "
                "Si dudas, corre `contenido-bionico status <run_id>` y espera mientras el "
                "heartbeat este fresco."
            ),
            f"- Comunidad oficial: {COMMUNITY_URL}.",
            help_text,
            "",
            "---",
            "",
        ]
    )


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[: idx + 1]), "".join(lines[idx + 1 :])
    return None


def _generated_frontmatter(skill: SkillSpec) -> str:
    return "\n".join(
        [
            "---",
            f"name: {skill.name}",
            f"description: {skill.description}",
            "---",
            "",
        ]
    )


def _codex_skill_metadata_ok(text: str, skill: SkillSpec) -> bool:
    split = _split_frontmatter(text)
    if split is None:
        return False
    frontmatter = split[0].lower()
    return f"name: {skill.name}".lower() in frontmatter and "description:" in frontmatter


def installable_skill_text(skill: SkillSpec, *, help_dir: Path | None = None) -> str:
    source_text = read_skill_text(skill)
    contract = _install_contract_text(help_dir=help_dir)
    split = _split_frontmatter(source_text)
    if split is not None:
        frontmatter, body = split
        return frontmatter.rstrip() + "\n\n" + contract + body.lstrip()
    return _generated_frontmatter(skill) + "\n" + contract + source_text.lstrip()


def codex_skill_text(skill: SkillSpec, *, help_dir: Path | None = None) -> str:
    text = installable_skill_text(skill, help_dir=help_dir)
    if not _codex_skill_metadata_ok(text, skill):
        raise ValueError(f"generated Codex skill is missing top YAML metadata: /{skill.name}")
    return text


def _claude_command_text(skill: SkillSpec, skill_text: str, *, help_dir: Path | None = None) -> str:
    help_line = f"Guias instaladas: `{help_dir}`." if help_dir else ""
    return "\n".join(
        [
            f"# /{skill.name}",
            "",
            (
                "Usa este flujo de contenido-bionico cuando el usuario escriba "
                f"/{skill.name}: {skill.description}."
            ),
            help_line,
            "",
            skill_text.rstrip(),
            "",
        ]
    )


def _sync_help_docs(target: str) -> None:
    """Mirror the packaged help docs into the target's help dir.

    Copies every source .md and removes destination .md files that no longer
    exist in the source, so renamed/deleted guides do not linger.
    """
    source = help_docs_source()
    if source is None:
        return
    destination = _codex_help_dir() if target == "codex" else _claude_help_dir()
    destination.mkdir(parents=True, exist_ok=True)
    wanted = {file.name for file in source.glob("*.md")}
    for file in source.glob("*.md"):
        shutil.copyfile(file, destination / file.name)
    for file in destination.glob("*.md"):
        if file.name not in wanted:
            try:
                file.unlink()
            except OSError as exc:
                print(f"No pude borrar la guia obsoleta {file}: {exc}", file=sys.stderr)


def legacy_artifact_paths(target: str) -> list[Path]:
    home = _home()
    if target == "claude":
        commands = home / ".claude" / "commands"
        paths = [commands / name for name in LEGACY_CLAUDE_COMMANDS]
        paths.extend(commands / name for name in LEGACY_CLAUDE_DIRS)
        return paths
    if target == "codex":
        skills = home / ".codex" / "skills"
        return [skills / name for name in LEGACY_CODEX_SKILL_DIRS]
    return []


def _remove_legacy_artifacts(target: str) -> None:
    for path in legacy_artifact_paths(target):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            print(f"No pude borrar el comando antiguo {path}: {exc}", file=sys.stderr)


def remaining_legacy_artifacts(target: str) -> list[str]:
    return [str(path) for path in legacy_artifact_paths(target) if path.exists()]


def _install_codex_skills() -> None:
    help_dir = _codex_help_dir()
    for skill in SKILLS:
        _write(codex_skill_path(skill.name), codex_skill_text(skill, help_dir=help_dir))
    _sync_help_docs("codex")
    _remove_legacy_artifacts("codex")


def _install_claude_commands() -> None:
    help_dir = _claude_help_dir()
    for skill in SKILLS:
        skill_text = installable_skill_text(skill, help_dir=help_dir)
        _write(claude_command_path(skill.name), _claude_command_text(skill, skill_text, help_dir=help_dir))
    _sync_help_docs("claude")
    _remove_legacy_artifacts("claude")


def install_bionico_skill(target: str = "auto") -> list[str]:
    installed: list[str] = []
    for resolved in resolve_targets(target):
        if resolved == "codex":
            try:
                _install_codex_skills()
                installed.append("Codex")
            except OSError as exc:
                print(f"No pude activar los skills de Bionico para Codex: {exc}", file=sys.stderr)
        elif resolved == "claude":
            try:
                _install_claude_commands()
                installed.append("Claude")
            except OSError as exc:
                print(f"No pude activar los comandos de Bionico para Claude: {exc}", file=sys.stderr)
    return installed


def _target_skill_paths(target: str) -> dict[str, Path]:
    if target == "codex":
        return {skill.name: codex_skill_path(skill.name) for skill in SKILLS}
    if target == "claude":
        return {skill.name: claude_command_path(skill.name) for skill in SKILLS}
    return {}


def installed_bionico_skill_status() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for target in ("codex", "claude"):
        missing: list[str] = []
        stale: list[str] = []
        paths = _target_skill_paths(target)
        for skill in SKILLS:
            path = paths[skill.name]
            if not path.exists():
                missing.append(f"/{skill.name}")
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stale.append(f"/{skill.name}")
                continue
            if SKILL_BUNDLE_VERSION not in text:
                stale.append(f"/{skill.name}")
                continue
            if target == "codex" and not _codex_skill_metadata_ok(text, skill):
                stale.append(f"/{skill.name}")
        legacy = remaining_legacy_artifacts(target)
        out[target] = {
            "installed": not missing and not stale,
            "missing": missing,
            "stale": stale,
            "paths": [str(path) for path in paths.values()],
            "display_name": target_display_name(target),
            # Additive keys (do not change the ones above): old commands from
            # previous releases that should be removed by a reinstall.
            "legacy_artifacts": legacy,
            "has_legacy_artifacts": bool(legacy),
        }
    return out


def installed_bionico_skill_targets() -> list[str]:
    return [
        target
        for target, status in installed_bionico_skill_status().items()
        if bool(status.get("installed"))
    ]
