"""Generic primitives for the SHORT deliverable PARTS registry.

`rerender_part(video_id, part)` re-renders ONE part's asset from its CURRENT
per-run intermediate inputs (whatever is on disk right now in
`runs/<video_id>/`) — it does NOT recompose the final video or publish
(that's `recompose`, below). The change agent edits a per-run intermediate
(or a per-run `remotion/Captions.tsx` copy, Task 3), then calls this
instead of hand-chaining a render recipe.

`recompose(video_id)` is the complementary, part-independent primitive: it
re-assembles the final video from the current manifest/intermediates,
re-layers audio, and publishes — the ONE glue step that replaces the
multi-step assemble/mix/publish recipe the change agent used to hand-chain.

Dispatch is driven by the registry (`short.change.registry.PARTS`):
`KNOWN_RERENDER_TAGS` below is asserted (in tests) to equal
`{p.rerender for p in PARTS.values() if p.rerender}` so the dispatch table
can never silently drift from the registry, the single source of truth.

Every render call is a thin wrapper around the SAME functions
`short.animate.orchestrator.run_orchestrator` already calls for
from-scratch production (`render_isolated_composition`,
`write_compositions_ts`, `_render_scene_remotion`), so production and
change-driven re-renders can never diverge.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from contenido_bionico.short.animate import orchestrator as orch
from contenido_bionico.short.change.registry import part_for

# Dispatch tags this module knows how to handle. Kept in sync with the
# registry by a test (`test_dispatch_tags_match_registry_rerender_tags`)
# that asserts this set equals `{p.rerender for p in PARTS.values() if
# p.rerender}` — the registry is the source of truth; this constant exists
# only so a mismatch fails loudly and locally instead of via a silent
# fallthrough to the `else: raise` branch.
KNOWN_RERENDER_TAGS = frozenset({"captions", "scene", "audio"})

# Per-segment render fingerprint: the sha256 of the Scene.tsx that produced the
# CURRENT animation.webm, persisted next to the webm at render time. A scene
# re-render compares it and SKIPS segments whose Scene.tsx did not change, so a
# look-only edit to one scene never re-renders every scene's webm. Runs rendered
# before this file existed fall back to an mtime comparison (webm newer than
# Scene.tsx = unchanged; fork_run's copytree preserves mtimes).
SCENE_TSX_HASH_FILE = "Scene.tsx.sha256"

# BIONICO_PRIMITIVE_QUIET (default 1): capture the verbose render/assemble/mix
# logs of each primitive into `runs/<id>/logs/primitives/` and print ONE status
# line per part instead. This is the biggest token lever for the change agent,
# whose Bash tool would otherwise stream every render log into its context.
# Set BIONICO_PRIMITIVE_QUIET=0 to stream full logs as before.
_QUIET_ENV = "BIONICO_PRIMITIVE_QUIET"


def _quiet_enabled() -> bool:
    raw = (os.environ.get(_QUIET_ENV) or "1").strip().lower()
    return raw not in {"0", "false", "no"}


@contextlib.contextmanager
def _captured_verbose_output(video_id: str, label: str):
    """Redirect OS-level stdout/stderr into a per-run log file for the duration.

    fd-level (`os.dup2`) so child-process output (ffmpeg/Remotion) is captured
    too, not just Python prints. Yields the log path, or None when capture is
    disabled (BIONICO_PRIMITIVE_QUIET=0) or unsupported (no real fds, e.g. some
    embedded interpreters) — in that case output streams through unchanged.
    Never raises: capture is an optimization, not a correctness requirement.
    """
    if not _quiet_enabled():
        yield None
        return
    try:
        log_dir = _rd(video_id) / "logs" / "primitives"
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 - capture must never break the render
        yield None
        return
    log_path = log_dir / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{label}.log"
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        out_fd = sys.stdout.fileno()
        err_fd = sys.stderr.fileno()
        saved_out = os.dup(out_fd)
        saved_err = os.dup(err_fd)
    except Exception:  # noqa: BLE001 - non-fd stdout (tests/embedded) -> no capture
        yield None
        return
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as fp:
            os.dup2(fp.fileno(), out_fd)
            os.dup2(fp.fileno(), err_fd)
            try:
                yield log_path
            finally:
                # Flush BEFORE restoring so buffered writes land in the log,
                # never leak to the restored stdout.
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                except Exception:  # noqa: BLE001
                    pass
                os.dup2(saved_out, out_fd)
                os.dup2(saved_err, err_fd)
    finally:
        try:
            os.close(saved_out)
        except OSError:
            pass
        try:
            os.close(saved_err)
        except OSError:
            pass


def _log_tail(path: Path, max_lines: int = 40, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(sin log)"
    lines = text.strip().splitlines()[-max_lines:]
    return "\n".join(lines)[-max_chars:]


def _run_quiet(video_id: str, label: str, fn: Callable[[], Any]):
    """Run `fn()` with its verbose output captured to a per-run log file.

    Returns `(value, log_path_or_None, elapsed_seconds)`. On failure the tail
    of the captured log is printed first (so the caller — usually the change
    agent — still sees the root cause without the full render stream), then the
    exception re-raises unchanged.
    """
    started = time.monotonic()
    log_path_holder: list[Path | None] = [None]
    try:
        with _captured_verbose_output(video_id, label) as log_path:
            log_path_holder[0] = log_path
            value = fn()
    except BaseException:
        captured = log_path_holder[0]
        if captured is not None:
            print(
                f"[{label}] FALLO tras {time.monotonic() - started:.0f}s; "
                f"ultimas lineas del log ({captured}):",
                flush=True,
            )
            print(_log_tail(captured), flush=True)
        raise
    return value, log_path_holder[0], time.monotonic() - started


def _status(label: str, text: str, log_path: Path | None) -> None:
    suffix = f" | log: {log_path}" if log_path else ""
    print(f"[{label}] {text}{suffix}", flush=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scene_render_reusable(
    seg_dir: Path, scene_tsx: Path, webm: Path, tsx_hash: str
) -> bool:
    """True when the segment's animation.webm is already rendered from the
    CURRENT Scene.tsx: the persisted hash matches (primary), or — for runs that
    predate hash tracking — the webm is at least as new as the Scene.tsx."""
    try:
        if not webm.exists() or webm.stat().st_size == 0:
            return False
        hash_path = seg_dir / SCENE_TSX_HASH_FILE
        if hash_path.exists():
            return hash_path.read_text(encoding="utf-8").strip() == tsx_hash
        return webm.stat().st_mtime >= scene_tsx.stat().st_mtime
    except OSError:
        return False


class PrimitiveError(RuntimeError):
    """Raised when `rerender_part` is asked to handle a part that has no
    rerender dispatch (manifest-only parts like `camera`, or respawn-only
    parts like `carousel`/`quotes`/`cut`), or whose required per-run
    intermediate is missing."""


def _rd(video_id: str) -> Path:
    # orch._rd is annotated `int` but only ever does `RUNS / str(video_id)`;
    # every real caller (main(), run_orchestrator's own args.video_id) already
    # passes the full run-id string (e.g. "7_short"), so this is safe.
    return orch._rd(video_id)  # type: ignore[arg-type]


def _rerender_captions(video_id: str) -> Path:
    """Re-render captions.webm from the PERSISTED captions_props.json.

    Deliberately reads the file as-is instead of calling
    `build_captions_props(video_id)`: the persisted JSON may carry a
    proofread correction (`--caption-correct`) or a change-agent hand edit
    that `build_captions_props` cannot reproduce (it is the pure/
    deterministic re-derivation used ONLY the first time captions are
    produced). Re-rendering must reflect exactly what's on disk now.

    `captions_props.json` is only written by production since Task 2, so a
    run produced earlier (or forked from one) has none. In that case,
    reconstruct it ONCE via `build_captions_props` (the same pure/
    deterministic derivation production itself uses), PERSIST it so this and
    every subsequent call reads the same on-disk file directly (preserving
    any proofread/hand edit made after this point), then proceed exactly as
    if the file had existed all along.
    """
    rd = _rd(video_id)
    props_path = rd / orch.CAPTIONS_PROPS_FILE
    if not props_path.exists():
        rebuilt = orch.build_captions_props(video_id)
        if rebuilt is None:
            raise PrimitiveError(
                f"no se puede re-renderizar captions: falta {props_path} y "
                f"el run {video_id!r} no tiene datos para reconstruirlo "
                "(sin transcript.json/source.mp4 o sin palabras)"
            )
        props_path.write_text(
            json.dumps(rebuilt, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        captions_props: dict[str, Any] = orch._read_json(props_path)
    except (OSError, ValueError) as exc:
        raise PrimitiveError(f"captions_props.json invalido: {exc}") from exc

    duration = captions_props.get("durationSec")
    out_webm = rd / "captions.webm"

    def _render() -> Path:
        return orch.render_isolated_composition(
            write_compositions=lambda p: orch.write_compositions_ts(
                run_id=video_id,
                scenes=[],
                captions_props=captions_props,
                out_path=p,
                run_dir=rd,
            ),
            composition_id="captions",
            out_webm=out_webm,
            expected_duration=duration,
            require_alpha=True,
        )

    rendered, log_path, took = _run_quiet(video_id, "rerender_captions", _render)
    _status("rerender_part", f"captions: OK ({took:.0f}s) -> {rendered}", log_path)
    return rendered


def _rerender_scene(video_id: str) -> Path | None:
    """Re-render ONLY the animations/<seg>/animation.webm whose Scene.tsx
    actually changed, from its CURRENT (possibly hand-edited) Scene.tsx.

    Scene selection: `rerender_part` takes no segment argument (it operates
    on the PART as a whole, mirroring captions). Change tracking is a
    per-segment content hash (`SCENE_TSX_HASH_FILE`, persisted next to
    animation.webm at render time): a segment whose Scene.tsx hash matches
    its persisted hash — or, for runs that predate hash tracking, whose webm
    is at least as new as its Scene.tsx — is SKIPPED and its existing webm
    reused, so a look-only edit to one scene renders exactly one scene.

    This is a LOOK-ONLY re-render: timing (duration_sec, words) is read back
    from the segment's own Scene_Context.json (`scene_request`, written by
    production's `_write_scene_context` and never touched by a look-only
    edit), never re-derived from the planner. A TIMING/content change is a
    re-plan and must go through the `scene` Part's respawn entry
    (`run_animate(edit=True, notes=...)`), not this primitive.

    Returns the last segment's (rendered or reused) webm path, or None if the
    run has no scenes (no animations/ dir, or no segment has both
    Scene_Context.json and Scene.tsx).
    """
    rd = _rd(video_id)
    animations_dir = rd / "animations"
    if not animations_dir.exists():
        return None

    # Segment dirs are always named by the integer segment_id (see
    # `_write_scene_context` / `_render_scene_remotion`); sort numerically so
    # "10" doesn't lexicographically land before "2". Falls back to the raw
    # name for a stray non-numeric dir instead of crashing the whole part.
    def _seg_sort_key(p: Path) -> tuple[int, str]:
        try:
            return (0, f"{int(p.name):020d}")
        except ValueError:
            return (1, p.name)

    last_webm: Path | None = None
    for seg_dir in sorted(
        (p for p in animations_dir.iterdir() if p.is_dir()),
        key=_seg_sort_key,
    ):
        scene_tsx = seg_dir / "Scene.tsx"
        context_path = seg_dir / "Scene_Context.json"
        if not scene_tsx.exists() or scene_tsx.stat().st_size == 0:
            continue
        if not context_path.exists():
            continue
        try:
            context = orch._read_json(context_path)
        except (OSError, ValueError) as exc:
            raise PrimitiveError(
                f"Scene_Context.json invalido para {seg_dir.name}: {exc}"
            ) from exc
        scene = context.get("scene_request")
        if not isinstance(scene, dict):
            raise PrimitiveError(
                f"Scene_Context.json de {seg_dir.name} no tiene scene_request"
            )

        webm = seg_dir / "animation.webm"
        tsx_hash = _file_sha256(scene_tsx)
        if _scene_render_reusable(seg_dir, scene_tsx, webm, tsx_hash):
            _status(
                "rerender_part",
                f"escena {seg_dir.name}: Scene.tsx sin cambios; se reutiliza animation.webm",
                None,
            )
            last_webm = webm
            continue

        def _render(sc: dict[str, Any] = scene) -> Path | None:
            return orch._render_scene_remotion(video_id, sc)

        rendered, log_path, took = _run_quiet(
            video_id, f"rerender_scene_{seg_dir.name}", _render
        )
        try:
            (seg_dir / SCENE_TSX_HASH_FILE).write_text(tsx_hash, encoding="utf-8")
        except OSError:
            pass  # fingerprint is an optimization; never fail the render for it
        _status(
            "rerender_part",
            f"escena {seg_dir.name}: re-renderizada ({took:.0f}s)",
            log_path,
        )
        last_webm = rendered
    return last_webm


def rerender_part(video_id: str, part: str) -> Path | None:
    """Re-render ONE part from its CURRENT per-run inputs.

    Does NOT recompose or publish — the caller (the change agent, or a
    human) runs `recompose(video_id)` next to assemble/mix/publish. Dispatch
    is driven by the registry's `Part.rerender` tag (`part_for(part)`): an
    unrecognized `part` key raises `KeyError` (part_for's own contract); a
    valid part with `rerender=None` (manifest-only parts like `camera`;
    respawn-only parts like `carousel`, `quotes`, `cut`) raises
    `PrimitiveError` instead of silently doing nothing.

    Returns the rendered artifact path, except for `audio` which returns
    None (audio has no picture to render here; `recompose`'s `mix()` step
    re-reads Audio_Plan.json), and `scene` when the run has no animated
    scenes to render. `scene` re-renders ONLY segments whose Scene.tsx
    changed since their last render (content-hash gate, mtime fallback).

    Verbose render logs are captured to `runs/<id>/logs/primitives/` and
    summarized to one status line per part (BIONICO_PRIMITIVE_QUIET=0
    streams them instead).
    """
    resolved = part_for(part)
    tag = resolved.rerender
    if tag is None:
        raise PrimitiveError(
            f"la parte {part!r} no tiene rerender (target={resolved.target!r}, "
            f"respawn={resolved.respawn!r}); usa el respawn correspondiente o "
            "recompose() si es manifest-only"
        )
    if tag == "captions":
        return _rerender_captions(video_id)
    if tag == "scene":
        return _rerender_scene(video_id)
    if tag == "audio":
        # No picture render here: recompose()'s mix() step re-reads
        # Audio_Plan.json and re-layers audio onto the current picture.
        return None
    # Unreachable if KNOWN_RERENDER_TAGS stays in sync with the registry
    # (guarded by test_dispatch_tags_match_registry_rerender_tags).
    raise PrimitiveError(f"tag de rerender desconocido: {tag!r}")


def recompose(video_id: str) -> Path | None:
    """Assemble the final video from the CURRENT per-run intermediates
    (manifest, captions.webm, animated scenes, camera plan),
    re-layer audio, and publish it. This is the ONE glue path: after editing
    any video-layer intermediate directly, or calling `rerender_part` for
    one or more parts, the change agent calls `recompose` exactly ONCE
    instead of hand-chaining assemble/mix/publish (the old multi-step
    recipe it used to copy-paste and run twice).

    Order matters and encapsulates a correctness gotcha the caller must
    never have to know about: `audio_mix.mix(run_dir)` is IDEMPOTENT via a
    `final.video_only.mp4` backup it treats as the canonical voice+video
    base whenever that file exists. If we assembled a fresh picture into
    `final.mp4` and then called `mix` while a STALE `final.video_only.mp4`
    from a previous recompose was still on disk, `mix` would silently
    re-layer audio onto the OLD base and discard the just-assembled
    picture. So the sequence is fixed:

        1. assemble_from_manifest(rd)      -- re-composite the picture
        2. unlink rd/final.video_only.mp4  -- drop the stale mix base, if any
        3. mix(rd)                         -- ONLY if Audio_Plan.json exists
        4. publish_output(...)             -- always

    Voice-only runs (no Audio_Plan.json, e.g. no music/SFX) skip step 3 and
    publish the assembled `final.mp4` directly; step 2 still runs so a
    stale backup from an earlier audio-enabled recompose can't linger.
    """
    rd = _rd(video_id)

    def _work() -> Path | None:
        from contenido_bionico.short.animate.assembly_render import (
            assemble_from_manifest,
        )

        assemble_from_manifest(rd)
        video_only = rd / "final.video_only.mp4"
        if video_only.exists():
            video_only.unlink()
        if (rd / "Audio_Plan.json").exists():
            from contenido_bionico.shared.audio.audio_mix import mix

            mix(rd)
        import contenido_bionico.pipeline as pl  # lazy: avoids import cycle

        return pl.publish_output(
            video_id, run_filename="final.mp4", kind="short", required=True
        )

    published, log_path, took = _run_quiet(video_id, "recompose", _work)
    _status("recompose", f"OK ({took:.0f}s): final publicado -> {published}", log_path)
    return published
