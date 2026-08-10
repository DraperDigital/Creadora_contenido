"""Load + validate audio_library/MANIFEST.json.

The author agent reads a compact view of this catalog to pick valid sfx_name
values. The audio mixer reads the same data to resolve sfx_name -> file path
and to apply the deterministic default gain from the manifest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class ManifestError(RuntimeError):
    """Raised when MANIFEST.json is missing, malformed, or references files
    that don't exist on disk."""


@dataclass(frozen=True)
class SfxEntry:
    name: str
    file: str
    category: str
    duration_seconds: float
    default_gain_db: float
    tags: list[str] = field(default_factory=list)
    when_to_use: str = ""

    def absolute_path(self, library_root: Path) -> Path:
        return (library_root / self.file).resolve()


@dataclass(frozen=True)
class MusicConfig:
    directory: str
    loop_strategy: str
    crossfade_seconds: float
    base_gain_db: float


@dataclass(frozen=True)
class Constraints:
    max_cues_per_segment: int
    min_cue_spacing_seconds: float


@dataclass(frozen=True)
class Manifest:
    version: int
    library_root: Path
    sfx_by_name: dict[str, SfxEntry]
    music: MusicConfig
    constraints: Constraints

    def sfx(self, name: str) -> SfxEntry:
        try:
            return self.sfx_by_name[name]
        except KeyError as exc:
            raise ManifestError(
                f"unknown sfx_name {name!r} (valid: {sorted(self.sfx_by_name)})"
            ) from exc

    def music_dir(self) -> Path:
        return (self.library_root / self.music.directory).resolve()

    def music_files(self) -> list[Path]:
        """Full recursive inventory of audio files under the music dir, used
        by doctor/setup to confirm the library ships at least one track. Note
        this is broader than what the selector picks from: selection always uses
        music/shorts (music/no-copyright is only a fallback when shorts is empty)
        plus tracks directly under the music dir as a last resort."""
        root = self.music_dir()
        if not root.exists():
            return []
        exts = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}
        return sorted(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in exts
        )


def _require(payload: dict, key: str, ctx: str) -> object:
    if key not in payload:
        raise ManifestError(f"{ctx}: missing required key {key!r}")
    return payload[key]


def _require_float(payload: dict, key: str, ctx: str) -> float:
    value = _require(payload, key, ctx)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{ctx}: {key!r} must be a number, got {value!r}") from exc


def _require_int(payload: dict, key: str, ctx: str) -> int:
    value = _require(payload, key, ctx)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{ctx}: {key!r} must be an int, got {value!r}") from exc


def _parse_sfx(entry: dict, library_root: Path, verify_files: bool) -> SfxEntry:
    name = _require(entry, "name", "sfx[]")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"sfx[]: name must be a non-empty string, got {name!r}")
    ctx = f"sfx[{name}]"
    file_rel = _require(entry, "file", ctx)
    if not isinstance(file_rel, str) or not file_rel:
        raise ManifestError(f"{ctx}: file must be a non-empty string")
    parsed = SfxEntry(
        name=name,
        file=file_rel,
        category=str(_require(entry, "category", ctx)),
        duration_seconds=_require_float(entry, "duration_seconds", ctx),
        default_gain_db=_require_float(entry, "default_gain_db", ctx),
        tags=[str(t) for t in entry.get("tags", []) if isinstance(t, str)],
        when_to_use=str(entry.get("when_to_use", "")),
    )
    if verify_files and not parsed.absolute_path(library_root).exists():
        raise ManifestError(
            f"{ctx}: file {parsed.absolute_path(library_root)} does not exist; "
            "drop the real audio file in place or leave it unresolved for the missing-audio report"
        )
    return parsed


def _parse_music(payload: dict) -> MusicConfig:
    ctx = "music"
    return MusicConfig(
        directory=str(_require(payload, "directory", ctx)),
        loop_strategy=str(payload.get("loop_strategy", "crossfade_loop")),
        crossfade_seconds=float(payload.get("crossfade_seconds", 2.0)),
        base_gain_db=_require_float(payload, "base_gain_db", ctx),
    )


def _parse_constraints(payload: dict) -> Constraints:
    ctx = "constraints"
    return Constraints(
        max_cues_per_segment=_require_int(payload, "max_cues_per_segment", ctx),
        min_cue_spacing_seconds=_require_float(payload, "min_cue_spacing_seconds", ctx),
    )


def load_manifest(library_root: Path, *, verify_files: bool = True) -> Manifest:
    """Load MANIFEST.json from `library_root` (the audio_library/ dir).

    With verify_files=True (default), every sfx[].file must exist on disk. Pass
    verify_files=False to inspect the manifest without requiring the audio
    binaries (useful in tests or in tooling that only renders the catalog).
    """
    library_root = library_root.resolve()
    path = library_root / "MANIFEST.json"
    if not path.exists():
        raise ManifestError(f"MANIFEST.json not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"MANIFEST.json is not valid JSON: {exc}") from exc

    version = _require_int(payload, "version", "manifest")
    sfx_list = _require(payload, "sfx", "manifest")
    if not isinstance(sfx_list, list) or not sfx_list:
        raise ManifestError("manifest: sfx[] must be a non-empty list")
    sfx_by_name: dict[str, SfxEntry] = {}
    for raw in sfx_list:
        if not isinstance(raw, dict):
            raise ManifestError(f"sfx[]: each entry must be a JSON object, got {type(raw).__name__}")
        entry = _parse_sfx(raw, library_root, verify_files)
        if entry.name in sfx_by_name:
            raise ManifestError(f"sfx[]: duplicate name {entry.name!r}")
        sfx_by_name[entry.name] = entry

    music_raw = _require(payload, "music", "manifest")
    if not isinstance(music_raw, dict):
        raise ManifestError("manifest: music must be a JSON object")
    music = _parse_music(music_raw)

    constraints_raw = payload.get("constraints", {})
    if not isinstance(constraints_raw, dict):
        raise ManifestError("manifest: constraints must be a JSON object")
    # Fallback mirrors the shipped MANIFEST.json so a manifest without a
    # constraints block behaves the same as the bundled library.
    constraints = _parse_constraints(
        constraints_raw or {
            "max_cues_per_segment": 40,
            "min_cue_spacing_seconds": 0.05,
        }
    )

    return Manifest(
        version=version,
        library_root=library_root,
        sfx_by_name=sfx_by_name,
        music=music,
        constraints=constraints,
    )


def manifest_for_author_prompt(manifest: Manifest) -> str:
    """Compact JSON the author agent reads inline in the prompt.

    Drops fields the author doesn't need to see (file paths, default_gain_db,
    and gain constraints which are auto-applied downstream). Keeps name,
    category, duration, tags,
    and when_to_use so the author has enough to pick the right SFX.
    """
    payload = {
        "selection_rules": [
            "Do not choose an SFX by list order, familiarity, or as a default.",
            "Use cues only when they reinforce a real visible motion or semantic event.",
            "Use pop only for instant small scale/bounce appearances.",
            "Use slide for translated or sliding entrances.",
            "Use fadein for opacity-only fade-ins.",
            "Use an empty cues array when no SFX is clearly justified.",
        ],
        "sfx": [
            {
                "name": s.name,
                "category": s.category,
                "duration_seconds": s.duration_seconds,
                "tags": s.tags,
                "when_to_use": s.when_to_use,
            }
            for s in manifest.sfx_by_name.values()
        ],
        "constraints": {
            "max_cues_per_segment": manifest.constraints.max_cues_per_segment,
            "min_cue_spacing_seconds": manifest.constraints.min_cue_spacing_seconds,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
