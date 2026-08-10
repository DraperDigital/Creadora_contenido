"""Codegen the shared/remotion/src/Compositions.generated.ts file (short format).

Esbuild bundles only what's statically imported, so per-run scene
components must be referenced via static imports. This module writes
a fresh `Compositions.generated.ts` per render call: it imports
every authored Scene tsx file (one per active segment),
then exports the COMPOSITIONS array that Root.tsx consumes.

Scene components are expected to live under
`shared/remotion/src/runs/<run_id>/<segment_id>/Scene.tsx`. The
caller (short orchestrator) is responsible for copying or writing
the authored Scene.tsx to that path BEFORE calling this module.

Thin wrapper: the actual implementation is the parameterized core in
``remotion_manifest_writer.py``; this module pins the short format
(1080x1920 canvas, Scene.tsx components).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# REMOTION_SRC and _namespace_static_assets are re-exported for back-compat
# (this module used to own the implementation).
from contenido_bionico.shared.remotion_manifest_writer import (  # noqa: F401
    DEFAULT_FPS,
    REMOTION_SRC,
    _copy_component_tsx,
    _namespace_static_assets,
    _write_manifest,
    copy_shared_component_tsx,
    stage_run_component,
)

SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920


class ManifestWriterError(RuntimeError):
    pass


def write_compositions_ts(
    *,
    run_id: int,
    fps: int = DEFAULT_FPS,
    scenes: list[dict[str, Any]] | None = None,
    captions_props: dict[str, Any] | None = None,
    out_path: Path | None = None,
    run_dir: Path | None = None,
) -> Path:
    """Write a Compositions TS module for one run.

    Args:
        run_id: numeric run id (used to namespace scene imports).
        fps: frames per second (defaults to DEFAULT_FPS).
        scenes: list of dicts with keys:
            - segment_id: int (1-indexed scene number)
            - duration_sec: float
            - default_props: SceneProps dict
        The orchestrator must have already copied each scene's Scene.tsx
        to `remotion/src/runs/<run_id>/<segment_id>/Scene.tsx`. None (the
        default) means no scene compositions (e.g. ranking overlay renders).
        captions_props: CaptionsProps payload, or None to skip the captions
            composition. When set, the Captions component is imported and a
            1080x1920 "captions" composition is appended.
        out_path: destination .ts file. Defaults to the shared
            `Compositions.generated.ts`; isolated renders pass a unique
            per-render path so concurrent renders never clobber each other.
        run_dir: this run's directory (`runs/<video_id>/`). When given, a per-run
            editable copy at `runs/<video_id>/remotion/Captions.tsx` (if
            present) is staged into the renderer tree and imported instead of the
            shared component. When None (the default) or when no per-run copy
            exists, the shared `./Captions` import is emitted —
            byte-for-byte today's behavior, so all existing callers are
            unaffected.

    Returns:
        Path to the generated .ts file.
    """
    captions_component_import: str | None = None
    if run_dir is not None:
        if captions_props is not None and stage_run_component(
            run_id=run_id, component="Captions", run_dir=run_dir
        ) is not None:
            captions_component_import = (
                f'import {{ Captions }} from "./runs/{run_id}/Captions";'
            )

    return _write_manifest(
        run_id=run_id,
        fps=fps,
        width=SHORT_WIDTH,
        height=SHORT_HEIGHT,
        component_basename="Scene",
        entries=scenes if scenes is not None else [],
        captions_props=captions_props,
        out_path=out_path,
        captions_component_import=captions_component_import,
    )


def copy_scene_tsx(
    *,
    run_id: int,
    segment_id: int,
    source_scene_tsx: Path,
    namespace_assets: bool = False,
) -> Path:
    """Place an authored Scene.tsx under remotion/src/runs/<run>/<seg>/.

    When `namespace_assets` is set, the scene's `staticFile("assets/...")` paths
    are rewritten to the per-run namespace `assets/runs/<run_id>/...` so the
    render loads each run's own staged images. Returns the destination path.
    """
    return _copy_component_tsx(
        run_id=run_id,
        segment_id=segment_id,
        source_tsx=source_scene_tsx,
        component_basename="Scene",
        namespace_assets=namespace_assets,
        error_cls=ManifestWriterError,
    )
