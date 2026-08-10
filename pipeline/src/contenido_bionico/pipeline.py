"""End-to-end pipeline: raw MP4 in, final MP4 out.

Wraps the two phase orchestrators (cut + animate) and the shared run-id
machinery into a single callable. The CLI in `cli.py` is a thin argparse
shim over this module.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from contenido_bionico.shared import config
from contenido_bionico.shared.cut.orchestrator import recut
from contenido_bionico.short.cut import orchestrator as short_cut_orchestrator
from contenido_bionico.short.animate import orchestrator as short_animate_orchestrator
from contenido_bionico.shared.ffmpeg import NO_WINDOW
from contenido_bionico.shared.run_ids import (
    RunKind,
    infer_video_kind,
    next_run_id,
    resolve_run_id,
    run_kind,
    run_number,
)


_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = _ROOT / "runs"
_PKG = Path(__file__).resolve().parent
_TRANSCRIBE_HELPER = _PKG / "short" / "cut" / "transcribe.py"
OUTPUT_KINDS = {
    "cortado": "cortado",
    "animado": "animado",
    "short": "short",
    "animado-full": "animado-full",
}
LEGACY_OUTPUT_SUFFIXES = ("source", "final")

# Containers the cut/full/animate pipeline supports as-is. The staged file is
# renamed to raw.mp4, so only mp4-family containers (plus MOV, same family)
# are safe; .webm/.mkv would need transcoding and are rejected up-front.
PIPELINE_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v")

# Generous floor for helper subprocess timeouts. Steps that know the media
# duration scale this up (10x media length) so very long videos never get
# killed early, while a hung process can no longer block the pipeline forever.
DEFAULT_STEP_TIMEOUT_SECONDS = 3600

# Bounds for detecting a transcript that describes a CUT video while only the
# uncut raw.mp4 is available. Trailing dead air on an uncut recording stays
# well inside these; a real cut removes far more.
_RAW_TRANSCRIPT_TOLERANCE_SECONDS = 30.0
_RAW_TRANSCRIPT_TOLERANCE_RATIO = 0.85


def _transcribe_helper(kind: RunKind) -> Path:
    return _TRANSCRIBE_HELPER


def next_video_id(kind: RunKind) -> str:
    """Reserve the next visible run id for a long/short pipeline.

    Not just a lookup: next_run_id also CREATES the run folder (os.mkdir) so
    two runs starting at the same time can never claim the same id.
    """
    return next_run_id(RUNS_DIR, kind)


def resolve_existing_run_id(value: str | int, *, expected_kind: RunKind | None = None) -> str:
    try:
        return resolve_run_id(value, RUNS_DIR, expected_kind=expected_kind)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _run_kind_for_video(path: Path, *, fallback: RunKind = "short") -> RunKind:
    try:
        return infer_video_kind(path)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[pipeline] could not detect orientation for {path}; using {fallback}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return fallback


def _output_kind_for_run(video_id: str | int, output_kind: str) -> RunKind:
    if output_kind == "short":
        return "short"
    detected = run_kind(video_id)
    return detected if detected is not None else "short"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _is_configured_inbox_file(path: Path) -> bool:
    input_dir = config.configured_input_dir()
    if input_dir is None:
        return False
    try:
        return path.resolve().parent == input_dir.resolve()
    except OSError:
        return False


def _require_transcript(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise SystemExit(f"la transcripcion no genero transcript.json en {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"transcript.json no es JSON valido: {path} ({exc})") from exc
    words = data.get("words") if isinstance(data, dict) else None
    if not isinstance(words, list) or not words:
        raise SystemExit(
            f"transcript.json no tiene words[] para planear la animacion: {path}"
        )


def _run_step(
    label: str,
    cmd: list[str],
    *,
    timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> None:
    """Run a helper subprocess, relaying its output as it happens.

    Output is streamed line-by-line (long steps like transcription used to sit
    in silence for minutes) and the process is killed after `timeout_seconds`.
    The last lines are kept for the failure message.
    """
    print(f"[pipeline] {label}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
        # POSIX: own session => own process group, so the timeout kill can
        # sweep grandchildren (ffmpeg) too. No-op on Windows.
        start_new_session=(os.name != "nt"),
    )
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        if os.name != "nt":
            # Kill the whole process group: proc.kill() alone leaves ffmpeg
            # grandchildren running (same pattern as runner_common).
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.kill()

    timer = threading.Timer(timeout_seconds, _kill_on_timeout)
    timer.daemon = True
    timer.start()
    tail: deque[str] = deque(maxlen=40)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            tail.append(line)
        rc = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise SystemExit(
            f"El paso '{label}' supero el tiempo limite de {int(timeout_seconds)} segundos "
            "y fue detenido. Reintenta; si vuelve a pasar, revisa tu conexion a internet "
            "o reporta el problema."
        )
    if rc != 0:
        tail_text = "".join(tail).strip()
        detail = f"\nUltimas lineas del paso:\n{tail_text}" if tail_text else ""
        raise SystemExit(f"El paso '{label}' fallo (codigo {rc}).{detail}")


def _media_duration_or_zero(path: Path) -> float:
    """Best-effort ffprobe duration; 0.0 when the media cannot be probed."""
    try:
        return ffprobe_duration(path)
    except SystemExit:
        return 0.0


def _step_timeout_for_media(path: Path) -> float:
    return max(DEFAULT_STEP_TIMEOUT_SECONDS, _media_duration_or_zero(path) * 10)


def ffprobe_duration(path: Path) -> float:
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
            timeout=60,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"ffprobe no pudo leer el video en {path}: {exc}") from exc
    try:
        return float(out.strip())
    except ValueError as exc:
        raise SystemExit(
            f"ffprobe devolvio una duracion no numerica para {path}: {out!r}"
        ) from exc


def _require_publishable_mp4(path: Path, label: str) -> float:
    if not path.exists():
        raise SystemExit(f"{label} no se genero en {path}")
    if path.stat().st_size <= 0:
        raise SystemExit(f"{label} quedo vacio en {path}")
    duration = ffprobe_duration(path)
    if duration <= 0:
        raise SystemExit(f"{label} tiene duracion invalida en {path}: {duration}")
    return duration


def _write_publish_report(
    video_id: str | int,
    *,
    kind: str,
    run_filename: str,
    published_path: Path,
    duration_seconds: float,
    status: str,
) -> None:
    run_dir = RUNS_DIR / str(video_id)
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "video_id": video_id,
        "kind": kind,
        "run_filename": run_filename,
        "published_path": str(published_path),
        "duration_seconds": duration_seconds,
        "status": status,
    }
    (logs_dir / "Publish_Report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def stage_raw_video(raw_mp4: Path, video_id: str | int) -> Path:
    """Stage the input MP4 as `runs/<id>/raw.mp4`, the canonical raw for the run.

    Always a COPY, never a move: the inbox original must survive this attempt
    so a watcher retry after a mid-run failure still finds its input (a move
    here made every retry fail with "no existe el archivo"). The watcher/agent
    own inbox cleanup once the job reaches a terminal-success state.
    """
    run_dir = RUNS_DIR / str(video_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / "raw.mp4"
    if not _same_path(dest, raw_mp4):
        shutil.copy2(raw_mp4, dest)
    return dest


def transcribe_raw_for_animation(
    video_id: str | int, raw_mp4: Path, *, kind: RunKind = "short"
) -> Path:
    """Create the transcript the animation planner needs without cutting the video."""
    run_dir = RUNS_DIR / str(video_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "_intermediates").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    transcript = run_dir / "transcript.json"
    if transcript.exists() and transcript.stat().st_size > 0:
        print(f"[pipeline] reusing transcript for animation: {transcript}", flush=True)
        _require_transcript(transcript)
        return transcript
    _run_step(
        "transcribe source video for animation planning",
        [
            sys.executable,
            str(_transcribe_helper(kind)),
            str(raw_mp4),
            "-o",
            str(transcript),
        ],
        timeout_seconds=_step_timeout_for_media(raw_mp4),
    )
    _require_transcript(transcript)
    return transcript


def _make_working_source_from_raw(raw_mp4: Path, source: Path) -> None:
    if source.exists():
        return
    try:
        os.link(raw_mp4, source)
    except OSError:
        shutil.copy2(raw_mp4, source)


def run_output_dir(video_id: str | int) -> Path | None:
    """The ONE output folder for a SHORT run: `<output_short>/run_<n>/`.

    Everything a short publishes lives here — the final video, the carousel
    slides, and the quote posts — so a run's deliverables are all in one place.
    `<n>` is the run number (e.g. `1_short` -> `run_1`). Returns None for
    non-short runs (longs keep their flat output) or when no output folder is
    configured.
    """
    if run_kind(video_id) != "short":
        return None
    base = config.configured_output_dir("short")
    if base is None:
        return None
    out = base / f"run_{run_number(video_id)}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def fork_run(parent_run_id: str | int) -> str:
    """Fork a finished SHORT run into a fresh run id for a versioned edit.

    Copies `runs/<parent>/` -> `runs/<new>/` EXCLUDING `logs/` (so the new
    version's token sum counts only the edit's own agent calls) and copies the
    parent's output folder `output_short/run_<parent#>` -> `run_<new#>` so the
    unchanged deliverables are already in place. The parent run is never
    touched, so its version stays downloadable. Returns the new run id
    (e.g. `'8_short'`).
    """
    parent = resolve_existing_run_id(parent_run_id, expected_kind="short")
    if run_kind(parent) != "short":
        raise SystemExit(f"solo se pueden versionar shorts; '{parent}' no es un short.")
    parent_dir = RUNS_DIR / str(parent)
    if not parent_dir.is_dir():
        raise SystemExit(f"no existe el run a editar: {parent_dir}")
    # next_run_id reserves the id by creating an empty folder; copytree merges
    # the parent's contents into it (dirs_exist_ok) while skipping logs/.
    new_id = next_run_id(RUNS_DIR, "short")
    shutil.copytree(
        parent_dir, RUNS_DIR / new_id,
        dirs_exist_ok=True, ignore=shutil.ignore_patterns("logs"),
    )
    base = config.configured_output_dir("short")
    if base is not None:
        parent_out = base / f"run_{run_number(parent)}"
        if parent_out.is_dir():
            new_out = base / f"run_{run_number(new_id)}"
            shutil.copytree(parent_out, new_out, dirs_exist_ok=True)
            # The final video's filename embeds the run number; rename the forked
            # copy to the new number so a video edit overwrites it in place
            # instead of leaving two `final_*.mp4` for collect_deliverables.
            stale_final = new_out / f"final_{run_number(parent)}.mp4"
            if stale_final.exists():
                stale_final.replace(new_out / f"final_{run_number(new_id)}.mp4")
    print(f"[pipeline] fork: {parent} -> {new_id}", flush=True)
    return new_id


def reclaim_run(run_id: str | int) -> None:
    """Remove a version's local working + output folders (per-version delete).

    Best-effort disk reclaim, driven by the agent when the dashboard deletes a
    version: drops `runs/<run_id>/` and its `output_short/run_<n>/` folder.
    Never raises — the version is already gone in the cloud regardless of local
    cleanup, and reclaiming one version never touches its siblings (each has its
    own forked run + output folder).
    """
    run = str(run_id)
    run_dir = RUNS_DIR / run
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
    try:
        base = config.configured_output_dir("short")
        number = run_number(run)
    except (ValueError, OSError):  # noqa: BLE001 - malformed id / no config: nothing to reclaim
        return
    if base is not None:
        out = base / f"run_{number}"
        if out.is_dir():
            shutil.rmtree(out, ignore_errors=True)


def run_change_on_fork(
    parent_run_id: str | int, target: str, notes: str, anim_opts: dict | None = None
) -> Path | None:
    """Apply a user change request to a finished short as a new version.

    Forks the parent run (so the original stays intact), hands the request to the
    change orchestrator (which decides + runs the right pipeline ops for the
    opened deliverable), and prints a `[pipeline] changed: <a,b>` line naming ONLY
    the deliverable(s) that changed so the agent publishes just those, plus the
    usual `[pipeline] done: <output folder>`. `anim_opts` carries the version's
    quality + feature snapshot for a re-animation.
    """
    from contenido_bionico.short.change.orchestrator import run_change

    new_id = fork_run(parent_run_id)
    result = run_change(new_id, target, notes, anim_opts=anim_opts)
    changed = result.get("changed") or []
    print(f"[pipeline] changed: {','.join(changed)}", flush=True)
    if result.get("summary"):
        print(f"[pipeline] summary: {result['summary']}", flush=True)
    if result.get("unsupported"):
        print(f"[pipeline] unsupported: {result['unsupported']}", flush=True)
    out = run_output_dir(new_id)
    if out is not None:
        print(f"[pipeline] done: {out}", flush=True)
    return out


def recut_and_rederive(
    video_id: str | int, notes: str | None, *, anim_opts: dict | None = None
) -> Path | None:
    """Full CUT re-entry for the change agent (registry `PARTS["cut"].respawn ==
    "recut_and_rederive"`): re-run the editorial cut with the user's notes, then
    re-derive EVERY transcript-dependent part and recompose/publish.

    This is the HEAVIEST change type. A re-cut regenerates `source.mp4` AND
    `transcript.json` (new duration + word times), which invalidates every
    transcript-derived part — caption cues, scene boundaries/timings, camera
    plan, and audio/SFX anchors are ALL defined against the transcript timeline.
    There is no stable mapping from old scene segments to new transcript spans,
    so a pinpointed re-derive is NOT safe: the only correct behavior is a FULL
    animate rebuild against the new transcript.

    Steps:
      1. `shared.cut.orchestrator.recut(video_id, notes)` — writes
         `recut_notes.txt`, clears the prior cut outputs, re-runs the
         editor/reviewer loop (notes present, deletion-only) and the
         deterministic tail -> new `source.mp4` + `transcript.json`.
      2. Full animate rebuild with `force=True`: captions and camera are
         re-derived deterministically from the new transcript, scenes are
         RE-PLANNED (force skips the cached `Scenes_Plan.json` whose boundaries
         no longer map) + re-authored + re-rendered at the new timing, and audio
         is rebuilt — then assemble + mix + publish. `build_extras=False`: the
         forked run already carries the parent's carousel/quotes (they are not
         transcript-timeline-bound), so they are reused, not regenerated.

    `anim_opts` (the version's quality + feature-toggle snapshot: `anim_quality`,
    `no_*` flags, `anim_count`) is threaded into the rebuild so the re-animation
    keeps this version's own settings. Returns the published final path (or None
    when no output folder is configured).

    IRON LAW gate: this is a full rebuild-from-scratch, which a change must
    never do — it is DISABLED unless the operator explicitly exports
    BIONICO_ALLOW_FULL_REBUILD=1. Cut changes go through `surgical_recut`.
    """
    allowed = (os.environ.get("BIONICO_ALLOW_FULL_REBUILD") or "").strip().lower()
    if allowed not in {"1", "true", "yes"}:
        raise SystemExit(
            "recut_and_rederive esta bloqueado (IRON LAW: un cambio nunca "
            "reconstruye el video completo). Usa pl.surgical_recut(video_id, "
            "notas) para cambios de corte. Para permitir una reconstruccion "
            "completa explicitamente, define BIONICO_ALLOW_FULL_REBUILD=1."
        )
    # Step 1: re-run the cut with the user's deletion-only notes. `recut` is a
    # module global (imported at top) so tests can monkeypatch `pipeline.recut`.
    recut(video_id, notes)
    # Step 2: full rebuild against the NEW transcript. force=True is REQUIRED —
    # the cached scene plan's `expanded-ok` marker survives the fork/re-cut, so
    # without force the planner is skipped and scenes would ship at their OLD
    # (now-wrong) timing. build_extras=False reuses the forked carousel/quotes.
    opts = dict(anim_opts or {})
    return run_short_phase(
        video_id,
        force=True,
        build_extras=False,
        **opts,
    )


def _validate_edl_ranges(ranges: list[tuple[float, float]]) -> None:
    """Guard an edited cut before we render from it: non-empty, each end>start,
    sorted ascending, non-overlapping. A malformed EDL would render a garbled
    source.mp4, so we fail loudly (surfaces to the dashboard) instead."""
    if not ranges:
        raise SystemExit("surgical_recut: el corte editado quedo vacio (sin rangos)")
    prev_end: float | None = None
    for start, end in ranges:
        if not (end > start):
            raise SystemExit(
                f"surgical_recut: rango invalido [{start}, {end}] (end <= start)"
            )
        if prev_end is not None and start < prev_end - 1e-6:
            raise SystemExit(
                f"surgical_recut: rangos solapados/desordenados en {start} (< {prev_end})"
            )
        prev_end = end


def surgical_recut(
    video_id: str | int, notes: str | None, *, anim_opts: dict | None = None
) -> Path | None:
    """SURGICAL cut re-entry for the change agent (registry `PARTS["cut"].respawn
    == "surgical_recut"`). The pinpointed replacement for `recut_and_rederive`.

    A cut change (swap a repeated take, drop a false start, tighten a silence,
    loosen a too-aggressive cut, remove a tangent) is applied by editing the
    EXISTING cut decision -- `_intermediates/edl_render.json`, the ordered list of
    kept source ranges -- NOT by re-running the editorial editor/reviewer/mapper
    loop and NOT by rebuilding the whole video. Only what the edit actually moved
    is re-derived.

    Steps:
      1. Snapshot the current kept ranges.
      2. Scoped `cut_editor` agent edits `edl_render.json` from the notes (ONE
         bounded call; no mapper/reviewer loop; bypasses the take-selection bug in
         `mapper.py` by letting the operator steer takes directly).
      3. Validate the edit landed and the ranges are well-formed.
      4. `resurface_from_edl` -> new `source.mp4` + `transcript.json`, rebuilt
         DETERMINISTICALLY from the edited ranges (regenerates the de-silenced
         working video if the fork dropped it).
      5. Remap `Scenes_Plan.json` onto the new timeline; invalidate ONLY the render
         artifacts of scenes that sit OVER the changed moment (so the no-force
         resume re-authors just those); every scene before/after is reused as-is.
      6. `run_short_phase(force=False, build_extras=False, **anim_opts)` re-derives
         captions/camera/audio deterministically, re-authors only the invalidated
         scenes, reuses the rest, then assembles + mixes + publishes.

    `anim_opts` (the version's quality + feature-toggle snapshot) is threaded into
    the phase so the re-derive keeps this version's own settings. Returns the
    published final path (or None when no output folder is configured).
    """
    from contenido_bionico.shared.cut import surgical_recut as sr
    from contenido_bionico.shared.cut.orchestrator import resurface_from_edl
    from contenido_bionico.short.animate.orchestrator import (
        _invalidate_one_scene_render_artifacts,
    )
    from contenido_bionico.short.change.cut_editor import (
        CutEditorDeclined,
        run_cut_editor,
    )

    if not (notes or "").strip():
        raise SystemExit("surgical_recut: el cambio de corte no puede estar vacio")
    run_dir = RUNS_DIR / str(video_id)
    edl_path = run_dir / "_intermediates" / "edl_render.json"
    plan_path = run_dir / "requests" / "Scenes_Plan.json"
    if not edl_path.exists():
        raise SystemExit(
            f"surgical_recut: no existe el corte editable {edl_path} "
            "(este video no tiene un edl_render.json para editar)"
        )

    # 1-2: snapshot the cut, then let the scoped editor apply the change in place.
    old_ranges = sr.load_ranges(edl_path)
    try:
        run_cut_editor(str(video_id), notes, run_dir)
    except CutEditorDeclined as exc:
        # Structured decline (deletion-only contract): surface the editor's
        # Spanish reason with the machine-readable DECLINADO marker. The change
        # orchestrator turns it into `unsupported`, and the engine posts
        # status='rejected' (amber card) instead of a red failure.
        print(f"[surgical_recut] DECLINADO: {exc.reason}", flush=True)
        raise SystemExit(f"DECLINADO: {exc.reason}") from exc
    new_ranges = sr.load_ranges(edl_path)

    # 3: the editor must have actually changed the cut, and to something valid.
    if new_ranges == old_ranges:
        raise SystemExit(
            "surgical_recut: el editor de corte no modifico el corte "
            "(el pedido quiza no era un cambio de corte o no se pudo aplicar)"
        )
    _validate_edl_ranges(new_ranges)

    # 4: deterministic rebuild of source.mp4 + transcript.json from the edited cut.
    resurface_from_edl(video_id)

    # 5: remap the scene plan onto the NEW timeline and re-author ONLY the scenes
    # that sit over the changed moment; scenes before/after keep their render.
    if plan_path.exists():
        remapped, affected, dropped = sr.remap_scenes_plan(
            plan_path, old_ranges, new_ranges
        )
        for seg_id in affected:
            _invalidate_one_scene_render_artifacts(video_id, seg_id)
        print(
            "[surgical_recut] escenas: %d reutilizadas, %d re-generadas, %d descartadas"
            % (len(remapped), len(affected), len(dropped)),
            flush=True,
        )

    # 6: re-derive captions/camera/audio, re-author just the invalidated scenes,
    # reuse the rest, assemble + mix + publish. force=False keeps the cached plan
    # (no LLM re-plan) and every untouched scene render.
    opts = dict(anim_opts or {})
    return run_short_phase(
        video_id,
        force=False,
        build_extras=False,
        **opts,
    )


def output_video_path(video_id: str | int, kind: str) -> Path | None:
    output_kind = _output_kind_for_run(video_id, kind)
    output_dir = config.configured_output_dir(output_kind)
    if output_dir is None:
        return None
    if kind == "short":
        # Shorts publish into a per-run folder alongside their carousel + quote
        # deliverables: output_short/run_<n>/final_<n>.mp4.
        run_dir = output_dir / f"run_{run_number(video_id)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"final_{run_number(video_id)}.mp4"
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = OUTPUT_KINDS[kind]
    return output_dir / f"contenido-bionico-{video_id}-{suffix}.mp4"


def _remove_old_outputs(video_id: str | int, keep: Path) -> None:
    output_dir = keep.parent
    stale_names = [
        f"contenido-bionico-{video_id}.mp4",
        *[
            f"contenido-bionico-{video_id}-{suffix}.mp4"
            for suffix in (*OUTPUT_KINDS.values(), *LEGACY_OUTPUT_SUFFIXES)
        ],
    ]
    for name in stale_names:
        stale = output_dir / name
        if _same_path(stale, keep):
            continue
        if stale.exists():
            stale.unlink()


def publish_output(
    video_id: str | int,
    *,
    run_filename: str,
    kind: str,
    required: bool = False,
) -> Path | None:
    """Move the user-facing MP4 to the configured output folder."""
    run_dir = RUNS_DIR / str(video_id)
    src = run_dir / run_filename
    if not src.exists():
        if required:
            raise SystemExit(f"el video {kind} no se genero en {src}")
        return None
    duration = _require_publishable_mp4(src, f"el video {kind}")
    dest = output_video_path(video_id, kind)
    if dest is None:
        print(
            f"[pipeline] output folder is not configured; leaving output at {src}",
            file=sys.stderr,
            flush=True,
        )
        _write_publish_report(
            video_id,
            kind=kind,
            run_filename=run_filename,
            published_path=src,
            duration_seconds=duration,
            status="left_in_run_dir",
        )
        return src
    if not _same_path(src, dest):
        _remove_old_outputs(video_id, keep=dest)
        if dest.exists():
            dest.unlink()
        shutil.move(str(src), str(dest))
    print(f"[pipeline] exported: {dest}", flush=True)
    _write_publish_report(
        video_id,
        kind=kind,
        run_filename=run_filename,
        published_path=dest,
        duration_seconds=duration,
        status="published",
    )
    return dest


# ---------- short secondary deliverables (carousel + quote posts) ----------
#
# After a SHORT publishes its final video, it also gets two extra deliverables,
# both built from the run's transcript, both published into the same
# `output_short/run_<n>/` folder as the final: a 4:5 "key points" carousel
# (PNG slides) and standalone quote-post mp4s. These run ONLY for shorts, never
# for longs, and are best-effort — a failure never fails the short.


def generate_and_publish_carousel(
    video_id: str | int, *, strict: bool = False, notes: str | None = None
) -> Path | None:
    """Build the carousel PNGs and publish them into the run's output folder.

    Slides land in `<output_short>/run_<n>/` as `carrusel_slide1.png`,
    `carrusel_slide2.png`, … alongside the final video and the quote posts.

    Auto path (`strict=False`): a carousel failure is logged and swallowed — the
    short video is the primary deliverable. Explicit `--carousel-run`
    (`strict=True`): re-raise so the command exits non-zero. Returns the run
    output folder, or None if it was skipped / left in the run dir.
    """
    try:
        from contenido_bionico.short.carousel.orchestrator import generate_carousel

        pngs = generate_carousel(video_id, notes=notes)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        print(
            f"[pipeline] carousel: generation failed ({type(exc).__name__}: {exc}); "
            "the video is unaffected",
            file=sys.stderr,
            flush=True,
        )
        return None

    out = run_output_dir(video_id)
    if out is None:
        print(
            f"[pipeline] carousel: {len(pngs)} slides left in run dir "
            "(no output folder configured)",
            flush=True,
        )
        return None
    for old in out.glob("carrusel_slide*.png"):
        old.unlink()
    ordered = sorted(pngs)
    for i, png in enumerate(ordered, start=1):
        shutil.copy2(png, out / f"carrusel_slide{i}.png")
    # Publish the carousel metadata alongside the slides so the run folder is
    # self-contained (used to build the post caption).
    if ordered:
        plan = ordered[0].parent / "Carousel_Plan.json"
        if plan.exists():
            shutil.copy2(plan, out / "carousel.json")
    print(f"[pipeline] carousel: exported {len(pngs)} slides -> {out}", flush=True)
    return out


def generate_and_publish_quotes(
    video_id: str | int, *, strict: bool = False, notes: str | None = None
) -> Path | None:
    """Build the quote-post mp4s and publish them into the run's output folder.

    Quotes land in `<output_short>/run_<n>/` as `quote_1.mp4`, `quote_2.mp4`, …
    Best-effort by default (a failure never fails the video); `strict=True`
    re-raises for the explicit `--quotes-run` command. Returns the run output
    folder, or None when skipped / left in the run dir / no quotes found.
    """
    try:
        from contenido_bionico.short.quote.build_quotes import generate_quotes

        mp4s = generate_quotes(video_id, notes=notes)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        print(
            f"[pipeline] quotes: generation failed ({type(exc).__name__}: {exc}); "
            "the video is unaffected",
            file=sys.stderr,
            flush=True,
        )
        return None

    out = run_output_dir(video_id)
    if out is None:
        print(
            f"[pipeline] quotes: {len(mp4s)} post(s) left in run dir "
            "(no output folder configured)",
            flush=True,
        )
        return None
    for old in out.glob("quote_*.mp4"):
        old.unlink()
    ordered = sorted(mp4s)
    for i, mp4 in enumerate(ordered, start=1):
        shutil.copy2(mp4, out / f"quote_{i}.mp4")
    if ordered:
        plan = ordered[0].parent / "Quote_Plan.json"
        if plan.exists():
            shutil.copy2(plan, out / "quotes.json")
    print(f"[pipeline] quotes: exported {len(mp4s)} post(s) -> {out}", flush=True)
    return out


def _build_extras_parallel(
    video_id: str | int, *, carousel: bool, quotes: bool
) -> None:
    """Run the enabled secondary deliverables (carousel, quotes) concurrently.

    Both are I/O-bound (an agent subprocess + a Remotion render each) and write
    to separate run subdirs and output filenames, so they run on threads. Each
    is best-effort and swallows its own errors, so the join never raises.
    """
    import concurrent.futures

    jobs = []
    if carousel:
        jobs.append(generate_and_publish_carousel)
    if quotes:
        jobs.append(generate_and_publish_quotes)
    if not jobs:
        return
    if len(jobs) == 1:
        jobs[0](video_id)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for fut in [pool.submit(job, video_id) for job in jobs]:
            fut.result()


def _build_run_caption(carousel_json: Path, quotes_json: Path) -> str:
    """Compose a post caption from the run's carousel + quote metadata.

    Hook = carousel title; body = one line per key point (`N. heading — body`);
    then `Frases:` with each quote. Both sources are optional.
    """
    lines: list[str] = []
    if carousel_json.exists():
        plan = json.loads(carousel_json.read_text(encoding="utf-8"))
        title = str(plan.get("title") or "").strip()
        if title:
            lines.append(title)
        items = [s for s in (plan.get("slides") or []) if s.get("kind") == "item"]
        if items:
            lines.append("")
            for s in items:
                heading = str(s.get("heading") or "").strip()
                body = str(s.get("body") or "").strip()
                idx = s.get("index")
                lines.append(f"{idx}. {heading} — {body}" if body else f"{idx}. {heading}")
    if quotes_json.exists():
        quotes = json.loads(quotes_json.read_text(encoding="utf-8")).get("quotes", [])
        texts = [(q.get("text") or "").strip() for q in quotes if (q.get("text") or "").strip()]
        if texts:
            lines.append("")
            lines.append("Frases:")
            lines.extend(f"— {t}" for t in texts)
    return ("\n".join(lines).strip() + "\n") if lines else ""


def _publish_run_caption(video_id: str | int) -> None:
    """Write `run_<n>/caption.txt` from the published carousel/quote metadata.

    Best-effort: a caption failure never fails the run.
    """
    out = run_output_dir(video_id)
    if out is None:
        return
    try:
        caption = _build_run_caption(out / "carousel.json", out / "quotes.json")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[pipeline] caption: skipped ({type(exc).__name__}: {exc})",
            file=sys.stderr,
            flush=True,
        )
        return
    if caption.strip():
        (out / "caption.txt").write_text(caption, encoding="utf-8")
        print(f"[pipeline] caption: wrote {out / 'caption.txt'}", flush=True)


def refresh_run_caption(video_id: str | int) -> None:
    """Public wrapper: rebuild `run_<n>/caption.txt` from the published
    carousel/quote metadata (used by --carousel-run/--quotes-run)."""
    _publish_run_caption(video_id)


def build_short_extras(
    video_id: str | int,
    *,
    carousel: bool = True,
    quotes: bool = True,
    formats_toggles: dict | None = None,
) -> None:
    """Build a short's secondary deliverables (carousel + quote posts + caption
    + the multi-format stage) into `output_short/run_<n>/`. No-op for non-short
    runs or when no output folder is configured. Best-effort: never fails the
    short.

    `carousel` / `quotes` (both default True) gate the two classic deliverables
    independently — the dashboard's Entregables section maps to these.
    `formats_toggles` gates the new per-category format groups (see
    `formats.registry.TOGGLE_FOR_CATEGORY`)."""
    if run_output_dir(video_id) is None:
        return
    _build_extras_parallel(video_id, carousel=carousel, quotes=quotes)
    _publish_run_caption(video_id)
    # The multi-format stage runs AFTER the classic caption: when the `textos`
    # toggle is on it overwrites caption.txt with the agent-written post caption;
    # when off, the classic metadata caption above stays as the fallback. Wrapped
    # so no format producer can ever fail the short.
    try:
        from contenido_bionico.short.formats.runner import run_formats_stage

        run_formats_stage(video_id, toggles=formats_toggles)
    except Exception as exc:  # noqa: BLE001 — formats stage never fails the short
        print(
            f"[pipeline] formats: skipped ({type(exc).__name__}: {exc})",
            file=sys.stderr,
            flush=True,
        )


def _transcript_last_word_end(transcript: Path) -> float | None:
    """Best-effort end timestamp (seconds) of the transcript's last word."""
    try:
        data = json.loads(transcript.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    words = data.get("words") if isinstance(data, dict) else None
    if not isinstance(words, list) or not words:
        return None
    last = words[-1]
    end = last.get("end") if isinstance(last, dict) else None
    try:
        return float(end)
    except (TypeError, ValueError):
        return None


def _require_transcript_matches_raw(
    video_id: str | int, raw_mp4: Path, transcript: Path
) -> None:
    """Abort before animating raw.mp4 against a cut-derived transcript.

    When the cut working video is gone, falling back to raw.mp4 with a
    transcript that describes the CUT video silently animates the uncut
    footage: every caption, animation and SFX lands at the wrong time. Two
    signals detect that case: the cut EDL artifacts in `_intermediates/`, and
    a raw duration much longer than the transcript's last word.
    """
    run_dir = RUNS_DIR / str(video_id)
    intermediates = run_dir / "_intermediates"
    edl_detected = any(
        (intermediates / name).exists()
        for name in ("edl_render.json", "edl_trimmed.json", "edl.json")
    )
    if edl_detected:
        raise SystemExit(
            f"El video cortado del run {video_id} ya no esta disponible y la transcripcion "
            f"({transcript}) corresponde a un video ya cortado, no al original (raw.mp4). "
            "Animar el video original con esa transcripcion dejaria todos los subtitulos, "
            "animaciones y efectos fuera de tiempo, asi que me detuve antes de arruinar el video.\n"
            "Solucion: vuelve a correr el corte sobre el video original (quedo guardado en "
            f"{run_dir / 'raw.mp4'}) y despues repite la animacion."
        )
    last_end = _transcript_last_word_end(transcript)
    raw_duration = _media_duration_or_zero(raw_mp4)
    if (
        last_end is not None
        and raw_duration > 0
        and raw_duration - last_end > _RAW_TRANSCRIPT_TOLERANCE_SECONDS
        and last_end < raw_duration * _RAW_TRANSCRIPT_TOLERANCE_RATIO
    ):
        # No cut EDL on disk: this is only a duration heuristic, so the
        # transcript MIGHT describe a cut video — or the original might just
        # end in music/silence without speech. Refuse, but say so.
        raise SystemExit(
            f"No encontre el video de trabajo (source.mp4) del run {video_id} y la "
            f"transcripcion ({transcript}) termina mucho antes que el video original "
            "(raw.mp4), como si describiera un video ya cortado. Animar el video "
            "original con esa transcripcion podria dejar los subtitulos, animaciones "
            "y efectos fuera de tiempo, asi que me detuve antes de arruinar el video.\n"
            "Solucion: si este run venia de un corte, vuelve a correr el corte sobre "
            f"el video original (quedo guardado en {run_dir / 'raw.mp4'}) y repite la "
            "animacion. Si tu video ya estaba editado y solo termina con musica o "
            "silencio sin voz (modo --animate-only), vuelve a lanzarlo con "
            f"`contenido-bionico \"{run_dir / 'raw.mp4'}\" --animate-only` para "
            "regenerar el video de trabajo y repetir la animacion."
        )


def restore_cut_for_animation(video_id: str | int) -> tuple[Path | None, str]:
    """Prepare the temporary `source.mp4` that the animation renderer expects."""
    run_dir = RUNS_DIR / str(video_id)
    source = run_dir / "source.mp4"
    if source.exists():
        return source, "existing"
    cut_output = output_video_path(video_id, "cortado")
    if cut_output is None or not cut_output.exists():
        raw_mp4 = run_dir / "raw.mp4"
        transcript = run_dir / "transcript.json"
        if raw_mp4.exists() and transcript.exists():
            _require_transcript(transcript)
            _require_transcript_matches_raw(video_id, raw_mp4, transcript)
            _make_working_source_from_raw(raw_mp4, source)
            print(f"[pipeline] prepared raw video for animation: {source}", flush=True)
            return source, "raw"
        return None, "missing"
    shutil.move(str(cut_output), str(source))
    print(f"[pipeline] restored cut video for animation: {source}", flush=True)
    return source, "cut_output"


def remove_working_video(video_id: str | int, run_filename: str) -> None:
    path = RUNS_DIR / str(video_id) / run_filename
    if path.exists():
        path.unlink()


def _prune_remotion_run_artifacts(video_id: str | int) -> None:
    """Best-effort: drop this run's Remotion codegen and staged assets from the
    installed source tree after a successful run.

    The manifest writer copies authored TSX under
    `shared/remotion/src/runs/<run>/` and asset staging mirrors images under
    `shared/remotion/public/assets/runs/<run>/`. Renders are isolated and the
    canonical copies live in `runs/<id>/`, so once the run succeeds these trees
    only accumulate forever inside the install.
    """
    try:
        from contenido_bionico.shared.remotion_runtime import REMOTION_PUBLIC
        from contenido_bionico.shared.remotion_manifest_writer import REMOTION_SRC

        for tree in (
            REMOTION_SRC / "runs" / str(video_id),
            REMOTION_PUBLIC / "assets" / "runs" / str(video_id),
        ):
            if tree.exists():
                shutil.rmtree(tree, ignore_errors=True)
                print(f"[pipeline] pruned remotion artifacts: {tree}", flush=True)
    except Exception as exc:  # noqa: BLE001 - pruning must never fail the run
        print(
            f"[pipeline] could not prune remotion artifacts for {video_id}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def run_cut(
    raw_mp4: Path,
    video_id: str | int | None = None,
    *,
    publish: bool = True,
    run_kind_override: RunKind | None = None,
) -> str:
    """Run phase 1 (cut). Returns the video_id assigned to this run."""
    if video_id is None:
        run_kind_value = run_kind_override or _run_kind_for_video(raw_mp4)
        video_id = next_video_id(run_kind_value)
    else:
        run_kind_value = run_kind_override or run_kind(video_id) or "short"
    staged = stage_raw_video(raw_mp4, video_id)
    print(f"[pipeline] cut: video_id={video_id} raw_mp4={staged}", flush=True)
    rc = short_cut_orchestrator.main([str(video_id), str(staged)])
    if rc != 0:
        raise SystemExit(f"la fase de corte fallo (codigo {rc}) para el video {video_id}")
    if publish:
        publish_output(video_id, run_filename="source.mp4", kind="cortado", required=True)
    return video_id


def run_animate(
    video_id: str | int,
    max_concurrency: int = 4,
    *,
    force: bool = False,
    notes: str | None = None,
    anim_quality: str | None = None,
    edit: bool = False,
    no_captions: bool = False,
    no_camera: bool = False,
    no_animations: bool = False,
    anim_count: str | None = None,
    no_music: bool = False,
    no_sfx: bool = False,
    no_carousel: bool = False,
    no_quotes: bool = False,
    no_videos_extra: bool = False,
    no_video_carruseles: bool = False,
    no_carruseles_extra: bool = False,
    no_imagenes: bool = False,
    no_textos: bool = False,
    no_posters: bool = False,
    build_extras: bool = True,
) -> Path | None:
    """Run phase 2 (animate). Assumes cut has already produced `runs/<id>/source.mp4`
    and `runs/<id>/transcript.json`.

    `edit=True` (pinpointed edit, short only): re-run the short with the user's
    `notes` re-doing only the scenes the change touches (see run_short_phase).
    `build_extras=False` (short only): reuse the run's existing carousel/quotes
    instead of regenerating them (a forked video edit already has them)."""
    if run_kind(video_id) != "short":
        raise SystemExit(
            f"solo se producen shorts; '{video_id}' no es un short."
        )
    return run_short_phase(
        video_id,
        max_concurrency=max_concurrency,
        force=force,
        notes=notes,
        anim_quality=anim_quality,
        edit=edit,
        no_captions=no_captions,
        no_camera=no_camera,
        no_animations=no_animations,
        anim_count=anim_count,
        no_music=no_music,
        no_sfx=no_sfx,
        no_carousel=no_carousel,
        no_quotes=no_quotes,
        no_videos_extra=no_videos_extra,
        no_video_carruseles=no_video_carruseles,
        no_carruseles_extra=no_carruseles_extra,
        no_imagenes=no_imagenes,
        no_textos=no_textos,
        no_posters=no_posters,
        build_extras=build_extras,
    )


def run_animation_only(
    raw_mp4: Path,
    video_id: str | int | None = None,
    max_concurrency: int = 4,
    *,
    run_kind_override: RunKind | None = None,
    force: bool = False,
) -> str:
    """Transcribe and animate a source video without running the cut/edit phase."""
    if video_id is None:
        video_id = next_video_id("short")
    return run_short_only(
        raw_mp4,
        video_id=video_id,
        max_concurrency=max_concurrency,
        force=force,
    )


# ---------- short (9:16) ----------

def run_short_phase(
    video_id: str | int,
    *,
    max_concurrency: int = 4,
    force: bool = False,
    notes: str | None = None,
    anim_quality: str | None = None,
    edit: bool = False,
    no_captions: bool = False,
    no_camera: bool = False,
    no_animations: bool = False,
    anim_count: str | None = None,
    no_music: bool = False,
    no_sfx: bool = False,
    no_carousel: bool = False,
    no_quotes: bool = False,
    no_videos_extra: bool = False,
    no_video_carruseles: bool = False,
    no_carruseles_extra: bool = False,
    no_imagenes: bool = False,
    no_textos: bool = False,
    no_posters: bool = False,
    build_extras: bool = True,
) -> Path | None:
    """Run the short phase only. Assumes cut produced source.mp4 + transcript.json.

    `edit=True` forwards --edit to the orchestrator: a pinpointed edit that, with
    `notes`, re-authors only the scenes the change touches (never forces).

    `build_extras=False` skips rebuilding the carousel/quote deliverables — used
    by a forked versioned VIDEO edit, whose extras were already copied from the
    parent run (rebuilding them would re-run those agents for no change)."""
    run_dir = RUNS_DIR / str(video_id)
    source = run_dir / "source.mp4"
    transcript = run_dir / "transcript.json"
    if not source.exists():
        # If the cut output was already published, move it back into the run dir.
        cut_output = output_video_path(video_id, "cortado")
        if cut_output is not None and cut_output.exists():
            shutil.move(str(cut_output), str(source))
        else:
            raw_mp4 = run_dir / "raw.mp4"
            if raw_mp4.exists() and transcript.exists():
                _require_transcript(transcript)
                _require_transcript_matches_raw(video_id, raw_mp4, transcript)
                _make_working_source_from_raw(raw_mp4, source)
            else:
                raise SystemExit(
                    f"la fase short necesita source.mp4 + transcript.json en {run_dir}"
                )

    print(f"[pipeline] short: video_id={video_id}", flush=True)
    argv = [
        "--video-id", str(video_id),
        "--max-concurrency", str(max_concurrency),
    ]
    if force:
        argv.append("--force")
    if notes:
        argv += ["--notes=" + notes]
    if edit:
        argv.append("--edit")
    if anim_quality:
        argv += ["--anim-quality", anim_quality]
    if no_captions:
        argv.append("--no-captions")
    if no_camera:
        argv.append("--no-camera")
    if no_animations:
        argv.append("--no-animations")
    if anim_count:
        argv += ["--anim-count", anim_count]
    if no_music:
        argv.append("--no-music")
    if no_sfx:
        argv.append("--no-sfx")
    rc = short_animate_orchestrator.main(argv)
    if rc != 0:
        raise SystemExit(f"la fase short fallo (codigo {rc}) para el video {video_id}")
    output = publish_output(video_id, run_filename="final.mp4", kind="short", required=True)
    # Every short also gets its secondary deliverables — a "key points" carousel
    # and quote-post mp4s — published into the same output_short/run_<n>/ folder.
    # The dashboard's Entregables toggles gate each one; both default on.
    # Best-effort: a failure here never fails the short itself. A forked video
    # edit passes build_extras=False: its carousel/quotes were copied from the
    # parent version unchanged, so it reuses them instead of regenerating.
    if build_extras:
        formats_toggles = {
            "videos_extra": not no_videos_extra,
            "video_carruseles": not no_video_carruseles,
            "carruseles_extra": not no_carruseles_extra,
            "imagenes": not no_imagenes,
            "posters": not no_posters,
            "textos": not no_textos,
            "quotes": not no_quotes,
        }
        build_short_extras(
            video_id,
            carousel=not no_carousel,
            quotes=not no_quotes,
            formats_toggles=formats_toggles,
        )
    # Keep `runs/<id>/source.mp4` around so --animate-run iterations don't have
    # to re-cut the raw. The short pipeline doesn't publish cortado, unlike
    # animate, so deleting source here would force a full cut on every rerun.
    _prune_remotion_run_artifacts(video_id)
    return output


def run_short(
    raw_mp4: Path,
    *,
    max_concurrency: int = 4,
    force: bool = False,
    anim_quality: str | None = None,
    no_captions: bool = False,
    no_camera: bool = False,
    no_animations: bool = False,
    anim_count: str | None = None,
    no_music: bool = False,
    no_sfx: bool = False,
    no_carousel: bool = False,
    no_quotes: bool = False,
    no_videos_extra: bool = False,
    no_video_carruseles: bool = False,
    no_carruseles_extra: bool = False,
    no_imagenes: bool = False,
    no_textos: bool = False,
    no_posters: bool = False,
) -> str:
    """Run cut then short. Returns the video_id."""
    video_id = run_cut(raw_mp4, publish=False, run_kind_override="short")
    output = run_short_phase(
        video_id,
        max_concurrency=max_concurrency,
        force=force,
        anim_quality=anim_quality,
        no_captions=no_captions,
        no_camera=no_camera,
        no_animations=no_animations,
        anim_count=anim_count,
        no_music=no_music,
        no_sfx=no_sfx,
        no_carousel=no_carousel,
        no_quotes=no_quotes,
        no_videos_extra=no_videos_extra,
        no_video_carruseles=no_video_carruseles,
        no_carruseles_extra=no_carruseles_extra,
        no_imagenes=no_imagenes,
        no_textos=no_textos,
        no_posters=no_posters,
    )
    if output is not None and output.exists():
        print(f"[pipeline] done: {output}", flush=True)
    else:
        final_mp4 = RUNS_DIR / str(video_id) / "final.mp4"
        print(
            f"[pipeline] done (no final output found at {final_mp4})",
            file=sys.stderr,
            flush=True,
        )
    return video_id


def run_short_only(
    raw_mp4: Path,
    *,
    video_id: str | int | None = None,
    max_concurrency: int = 4,
    force: bool = False,
) -> str:
    """Transcribe a vertical source as-is and produce a short without cutting."""
    if video_id is None:
        video_id = next_video_id("short")
    staged = stage_raw_video(raw_mp4, video_id)
    print(f"[pipeline] short-only: video_id={video_id} raw_mp4={staged}", flush=True)
    transcribe_raw_for_animation(video_id, staged, kind="short")
    run_dir = RUNS_DIR / str(video_id)
    _make_working_source_from_raw(staged, run_dir / "source.mp4")
    run_short_phase(
        video_id,
        max_concurrency=max_concurrency,
        force=force,
    )
    return video_id
