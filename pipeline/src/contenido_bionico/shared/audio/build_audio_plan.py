"""Build runs/<id>/Audio_Plan.json from author-selected sound cues.

Input:
    runs/<id>/animations/<seg_id>/Sound_Cues.json
    runs/<id>/Music_Override.json   (optional, see below)

The author-selected path resolves cue requests against
audio_library/MANIFEST.json. Unknown or missing sounds are reported instead of
aborting the video; only resolved cues with existing files are mixed.

Music selection is random per video (seeded by the run id) from the
length-appropriate pool. To force a specific track for one run, write
runs/<id>/Music_Override.json with {"music_file": "music/shorts/x.mp3"}
(relative to audio_library/, or an absolute path). Audio_Plan.json itself is
regenerated on every pass, so edits there do not survive; the override file
does. The --music CLI flag takes precedence over the override file.

The first random pick is pinned in runs/<id>/Music_Pick.json and reused on
re-plans, so re-planning the same video never flips its track; delete that
file (or use an override) to re-roll.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from contenido_bionico.shared.ffmpeg import (
    MUSIC_TARGET_LUFS,
    NO_WINDOW as _NO_WIN,
    static_gain_db,
)
from contenido_bionico.shared.run_ids import run_number

from .loudness_cache import cached_lufs
from .manifest import Manifest, ManifestError, load_manifest
from .validate_cues import CueValidationError, resolve_cues_payload

_MUSIC_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}

# Background music is always picked from the `shorts/` pool: the short pipeline
# is the only pathway now, so length no longer selects the pool. `no-copyright/`
# remains a fallback used only when `shorts/` is empty. (Previously videos over
# SHORT_MAX_SECONDS pulled from `no-copyright/`, which leaked that pool into 60s+
# shorts and into every format that inherits the Audio_Plan music pick.)
SHORT_MAX_SECONDS = 60.0
_SHORTS_MUSIC_SUBDIR = "shorts"
_LONG_MUSIC_SUBDIR = "no-copyright"

# Per-run music override written by the user next to Audio_Plan.json. It
# survives re-runs (Audio_Plan.json itself is regenerated on every pass).
_MUSIC_OVERRIDE_FILENAME = "Music_Override.json"

# Per-run pin of the RANDOM music pick, written after the first successful
# plan. The seeded RNG alone is deterministic, but the recent-picks collision
# re-roll depends on OTHER videos' picks, so a later re-plan could flip the
# track; the pin freezes the first pick. Best-effort, superseded by
# --music / Music_Override.json.
_MUSIC_PICK_FILENAME = "Music_Pick.json"

# Repeat-avoidance sidecar next to the library MANIFEST: the last few random
# music picks. When the seeded pick collides with a recent pick from a
# DIFFERENT video, it is re-rolled ONCE (still deterministic per run id;
# re-planning the SAME video never flips its own track). Best-effort: an
# unreadable or unwritable sidecar never blocks a plan.
_RECENT_PICKS_FILENAME = ".recent_picks.json"
_RECENT_PICKS_KEEP = 5


class BuildAudioPlanError(RuntimeError):
    pass


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
            creationflags=_NO_WIN,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise BuildAudioPlanError(f"ffprobe could not read {path}: {exc}") from exc
    try:
        return float(out.strip())
    except ValueError as exc:
        raise BuildAudioPlanError(
            f"ffprobe returned non-numeric duration for {path}: {out!r}"
        ) from exc


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise BuildAudioPlanError(f"required file does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildAudioPlanError(f"{path}: not valid JSON ({exc})") from exc


def _music_candidates(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _MUSIC_EXTS
    )


def _music_override(run_dir: Path, library_root: Path) -> Path | None:
    """Resolve runs/<id>/Music_Override.json, the per-run music override.

    Expected shape: {"music_file": "music/shorts/x.mp3"} where the path is
    relative to audio_library/ or absolute. Returns None when there is no
    override file; raises a clear error when the file exists but is unusable,
    so a user who asked for a specific track never gets a silent random pick.
    """
    override_path = run_dir / _MUSIC_OVERRIDE_FILENAME
    if not override_path.exists():
        return None
    payload = _load_json(override_path)
    raw = payload.get("music_file") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise BuildAudioPlanError(
            f'{override_path}: falta la clave "music_file" con la ruta de la pista '
            "(relativa a audio_library/ o absoluta)"
        )
    candidate = Path(raw.strip()).expanduser()
    resolved = (candidate if candidate.is_absolute() else library_root / candidate).resolve()
    if not resolved.exists():
        raise BuildAudioPlanError(
            f'{override_path}: la pista "{raw.strip()}" no existe (busque en {resolved}); '
            "corrige la ruta o borra Music_Override.json para volver a la seleccion aleatoria"
        )
    return resolved


def _pinned_music_pick(run_dir: Path) -> Path | None:
    """Best-effort read of runs/<id>/Music_Pick.json (the pinned random pick).

    Returns None when the pin is absent, unreadable, malformed, or points at a
    track that no longer exists — the caller then falls back to a fresh pick.
    """
    path = run_dir / _MUSIC_PICK_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = payload.get("music_file") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw.strip())
    if not candidate.is_absolute() or not candidate.exists():
        return None
    return candidate


def _save_music_pick(run_dir: Path, music_file: Path) -> None:
    try:
        (run_dir / _MUSIC_PICK_FILENAME).write_text(
            json.dumps(
                {"music_file": str(music_file)}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # pinning is best-effort; the seeded pick still usually repeats


def _pick_key(pick: Path, library_root: Path) -> str:
    """Stable sidecar key for a picked track: library-relative posix path when
    the track lives inside the library, else its absolute path."""
    resolved = pick.resolve()
    try:
        return resolved.relative_to(library_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _load_recent_picks(library_root: Path) -> list[dict]:
    """Entries of `.recent_picks.json`, each {"video_id": str|None, "file": str}.

    Plain-string entries (older sidecars) are accepted as picks from an
    unknown video.
    """
    path = library_root / _RECENT_PICKS_FILENAME
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError covers JSON and UTF-8 decode errors
        return []
    recent = payload.get("recent") if isinstance(payload, dict) else None
    if not isinstance(recent, list):
        return []
    entries: list[dict] = []
    for item in recent:
        if isinstance(item, str):
            entries.append({"video_id": None, "file": item})
        elif isinstance(item, dict) and isinstance(item.get("file"), str):
            entries.append({"video_id": item.get("video_id"), "file": item["file"]})
    return entries


def _record_music_pick(library_root: Path, video_id: str, key: str) -> None:
    # One entry per video: re-planning the same video refreshes its entry
    # instead of flooding the window with duplicates.
    recent = [
        entry
        for entry in _load_recent_picks(library_root)
        if entry.get("video_id") != video_id
    ]
    recent.append({"video_id": video_id, "file": key})
    try:
        (library_root / _RECENT_PICKS_FILENAME).write_text(
            json.dumps(
                {"recent": recent[-_RECENT_PICKS_KEEP:]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # repeat-avoidance is best-effort


def _pick_music_file(
    library: Manifest, video_id: str, duration_seconds: float
) -> Path | None:
    """Pick a background track from the `shorts/` pool.

    Every video draws from `music/shorts/` regardless of length (the short
    pipeline is the only pathway now). Falls back to `music/no-copyright/` and
    then to any track directly under the music dir, so a misconfigured library
    still yields music when possible. `duration_seconds` is accepted for
    signature stability but no longer selects the pool. Picks that collide with
    one of the last `_RECENT_PICKS_KEEP` picks (sidecar `.recent_picks.json`)
    are re-rolled once with the same seeded RNG.
    """
    music_dir = library.music_dir()
    primary = _SHORTS_MUSIC_SUBDIR
    fallback = _LONG_MUSIC_SUBDIR

    candidates = _music_candidates(music_dir / primary)
    if not candidates:
        candidates = _music_candidates(music_dir / fallback)
    if not candidates:
        candidates = _music_candidates(music_dir)
    if not candidates:
        return None
    try:
        seed = run_number(video_id)
    except (TypeError, ValueError):
        seed = abs(hash(video_id)) & 0xFFFFFFFF
    rng = random.Random(seed)
    pick = candidates[rng.randrange(len(candidates))]
    recent = _load_recent_picks(library.library_root)
    key = _pick_key(pick, library.library_root)
    # Only picks recorded by OTHER videos count as collisions, so re-planning
    # the same video keeps its own (deterministic) track.
    if any(
        entry["file"] == key and entry.get("video_id") != video_id
        for entry in recent
    ):
        pick = candidates[rng.randrange(len(candidates))]
    _record_music_pick(
        library.library_root, video_id, _pick_key(pick, library.library_root)
    )
    return pick


def _segment_time_start_map(assembly: dict) -> dict[int, tuple[float, float]]:
    segments = assembly.get("segments") or []
    out: dict[int, tuple[float, float]] = {}
    for seg in segments:
        try:
            sid = int(seg.get("segment_id"))
            ts = float(seg.get("time_start"))
            te = float(seg.get("time_end"))
        except (TypeError, ValueError):
            continue
        out[sid] = (ts, te)
    return out


def _cue_to_report(cue: object) -> dict:
    payload = asdict(cue)  # dataclass from validate_cues.py
    if isinstance(payload.get("file"), Path):
        payload["file"] = str(payload["file"])
    return payload


def _resolve_sound_cues(
    *,
    run_dir: Path,
    manifest: Manifest,
    segment_id: int,
    time_start: float,
    time_end: float,
) -> tuple[list[dict], list[dict], list[str]]:
    seg_dir = run_dir / "animations" / str(segment_id)
    cue_path = seg_dir / "Sound_Cues.json"
    warnings: list[str] = []
    if not cue_path.exists():
        return [], [], []

    try:
        payload = json.loads(cue_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [
            {
                "segment_id": segment_id,
                "requested_sound": "invalid Sound_Cues.json",
                "reason": f"JSON parse error: {exc}",
                "source": str(cue_path),
            }
        ], [f"seg {segment_id}: Sound_Cues.json invalid ({exc})"]

    duration = max(time_end - time_start, 0.0)
    try:
        resolution = resolve_cues_payload(
            payload,
            manifest,
            segment_duration_seconds=duration,
            library_root=manifest.library_root,
        )
    except CueValidationError as exc:
        return [], [
            {
                "segment_id": segment_id,
                "requested_sound": "invalid Sound_Cues payload",
                "reason": str(exc),
                "source": str(cue_path),
            }
        ], [f"seg {segment_id}: Sound_Cues validation failed ({exc})"]

    warnings.extend(f"seg {segment_id}: {w}" for w in resolution.warnings)
    resolved: list[dict] = []
    for cue in resolution.resolved:
        resolved.append(
            {
                "segment_id": segment_id,
                "sfx_name": cue.sfx_name,
                "requested_sfx_name": cue.requested_sfx_name,
                "file": str(cue.file),
                "absolute_seconds": round(time_start + cue.offset_seconds, 4),
                "gain_db": cue.gain_db,
                "kind": "author_selected",
                "intent": cue.intent,
                "match_type": cue.match_type,
            }
        )
    unresolved = [_cue_to_report(cue) for cue in resolution.unresolved]
    for cue in unresolved:
        cue["source"] = str(cue_path)
    return resolved, unresolved, warnings


def build_audio_plan(
    run_dir: Path,
    library_root: Path,
    *,
    force_music_file: Path | None = None,
    include_music: bool = True,
    include_sfx: bool = True,
) -> Path:
    """Build runs/<id>/Audio_Plan.json. Returns the written path.

    `include_music` / `include_sfx` (both default True) choose which layers go
    into the plan. `include_music=False` omits the music block entirely (no
    override or random pick); `include_sfx=False` skips per-segment SFX cue
    resolution. The consuming audio_mix step no-ops when both layers are gone.
    """
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise BuildAudioPlanError(f"run dir does not exist: {run_dir}")
    video_id = run_dir.name

    try:
        manifest = load_manifest(library_root, verify_files=False)
    except ManifestError as exc:
        raise BuildAudioPlanError(f"audio library manifest: {exc}") from exc

    assembly = _load_json(run_dir / "Assembly_Instructions.json")
    segment_timing = _segment_time_start_map(assembly)
    # Segments are only needed to anchor SFX cues; music-only plans (e.g. a
    # no-animations run with music on) don't require any.
    if include_sfx and not segment_timing:
        raise BuildAudioPlanError(
            f"Assembly_Instructions.json has no usable segments at {run_dir}"
        )

    final_path = run_dir / "final.mp4"
    source_path = run_dir / "source.mp4"
    if final_path.exists():
        duration = _probe_duration(final_path)
    elif source_path.exists():
        duration = _probe_duration(source_path)
    else:
        raise BuildAudioPlanError(
            f"neither final.mp4 nor source.mp4 found in {run_dir} - cannot probe duration"
        )

    sfx_cues: list[dict] = []
    unresolved_cues: list[dict] = []
    warnings: list[str] = []
    if include_sfx:
        for seg_id, (time_start, time_end) in sorted(segment_timing.items()):
            resolved, unresolved, cue_warnings = _resolve_sound_cues(
                run_dir=run_dir,
                manifest=manifest,
                segment_id=seg_id,
                time_start=time_start,
                time_end=time_end,
            )
            sfx_cues.extend(resolved)
            unresolved_cues.extend(unresolved)
            warnings.extend(cue_warnings)

        sfx_cues.sort(key=lambda c: (c["absolute_seconds"], c["segment_id"]))

    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if warnings:
        (logs / "Audio_Plan_Warnings.log").write_text(
            "\n".join(warnings) + "\n",
            encoding="utf-8",
        )
    if unresolved_cues:
        (logs / "Missing_Audio_Cues.json").write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "unresolved_cues": unresolved_cues,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    pick_to_pin: Path | None = None
    if not include_music:
        music_file = None
    elif force_music_file is not None:
        music_file = force_music_file.resolve()
        if not music_file.exists():
            raise BuildAudioPlanError(f"forced music file does not exist: {music_file}")
    else:
        music_file = _music_override(run_dir, manifest.library_root)
        if music_file is None:
            # A pinned pick from a previous plan of THIS run wins over a fresh
            # random pick, so re-planning never flips the track.
            music_file = _pinned_music_pick(run_dir)
        if music_file is None:
            music_file = _pick_music_file(manifest, video_id, duration)
            pick_to_pin = music_file

    if music_file is None:
        music_block = None
    else:
        # Per-track measured gain: bring THIS track to MUSIC_TARGET_LUFS
        # (voice target + music offset) instead of the manifest's flat gain,
        # so quiet and loud library tracks land at the same level under the
        # voice. Measurements are cached in the library loudness sidecar.
        # Unmeasurable tracks fall back to the manifest's flat base gain.
        track_lufs = cached_lufs(music_file, manifest.library_root)
        if track_lufs is None:
            music_gain_db = manifest.music.base_gain_db
        else:
            music_gain_db = static_gain_db(track_lufs, MUSIC_TARGET_LUFS)
        music_block = {
            "file": str(music_file),
            "base_gain_db": music_gain_db,
            "measured_track_lufs": track_lufs,
            "loop_strategy": manifest.music.loop_strategy,
            "crossfade_seconds": manifest.music.crossfade_seconds,
            "intro_fade_seconds": 1.5,
            "outro_fade_seconds": 2.0,
        }

    plan = {
        "video_id": video_id,
        "source_duration_seconds": round(duration, 4),
        "library_root": str(manifest.library_root),
        "music": music_block,
        "sfx_cues": sfx_cues,
        "unresolved_cues": unresolved_cues,
        "warnings": warnings,
    }

    out_path = run_dir / "Audio_Plan.json"
    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if pick_to_pin is not None:
        _save_music_pick(run_dir, pick_to_pin.resolve())
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", help="The video id (run dir name).")
    parser.add_argument(
        "--library",
        type=Path,
        default=None,
        help="Path to audio_library/. Defaults to <repo_root>/audio_library.",
    )
    parser.add_argument(
        "--music",
        type=Path,
        default=None,
        help="Force a specific music file (overrides Music_Override.json and random selection).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[4]
    run_dir = repo_root / "runs" / str(args.video_id)
    library_root = args.library or (repo_root / "audio_library")

    try:
        out_path = build_audio_plan(
            run_dir,
            library_root,
            force_music_file=args.music,
        )
    except BuildAudioPlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
