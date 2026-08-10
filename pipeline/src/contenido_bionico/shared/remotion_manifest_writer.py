"""Codegen the Remotion composition manifests (shared core).

The Remotion bundler only includes statically imported files, so each authored
component TSX file is copied under shared/remotion/src/runs/<run>/<segment>/
and then referenced from a generated Compositions TS module.

This module owns the SINGLE parameterized implementation (canvas size, fps,
component basename, optional captions entry). The short entry points in
``remotion_manifest_short.py`` are thin wrappers over this core (Scene.tsx,
1080x1920).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


REMOTION_SRC = Path(__file__).resolve().parent / "remotion" / "src"

# Single Python-side source of truth for the render frame rate. Mirrors FPS in
# shared/remotion/src/lib/config.ts — keep the two in sync.
DEFAULT_FPS = 30


class RemotionManifestError(RuntimeError):
    pass


def _ts_string(value: Any) -> str:
    """Serialize a Python value as a JSON literal safe to embed in TS."""
    return json.dumps(value, ensure_ascii=False)


# --- shared core -------------------------------------------------------------


_STATICFILE_ASSET_RE = re.compile(
    r'staticFile\(\s*(["\'])assets/(?!runs/)([^"\']+)\1\s*\)'
)


def _namespace_static_assets(tsx: str, run_id: str) -> str:
    """Rewrite `staticFile("assets/<x>")` -> `staticFile("assets/runs/<run>/<x>")`.

    Already-namespaced paths (`assets/runs/...`) are left untouched, so this is
    idempotent. Matches the per-run asset layout (canonical in the run dir,
    staged into public/assets/runs/<run>/).
    """
    return _STATICFILE_ASSET_RE.sub(
        lambda m: f"staticFile({m.group(1)}assets/runs/{run_id}/{m.group(2)}{m.group(1)})",
        tsx,
    )


# Shared components a run may override with a per-run editable COPY. Captions
# is exported from Captions.tsx. The per-run copy is opt-in: production never
# creates one (so the shared component renders byte-for-byte as today), the
# change agent creates it to unlock any off-prop-surface attribute (shadow,
# outline, weight, underline, entrance).
SHARED_COMPONENT_SOURCES = {
    "Captions": REMOTION_SRC / "Captions.tsx",
}

# Matches a relative-import specifier (`./x`, `../x`) in an `import ... from
# "..."` statement. Bare specifiers (react, remotion, @remotion/...) don't match
# and are left untouched — they resolve from node_modules regardless of depth.
_RELATIVE_IMPORT_RE = re.compile(
    r'(from\s*)(["\'])(\.{1,2}/[^"\']*)\2'
)


def _rewrite_relative_import_depth(tsx: str, extra_depth: int) -> str:
    """Prefix `extra_depth` levels of `../` onto every relative import.

    A per-run component copy is staged one or more directories DEEPER than the
    shared source it was copied from, so its `./lib/types`-style imports would no
    longer resolve. Re-anchoring each relative specifier with `../` * extra_depth
    restores the original target from the deeper location (e.g. depth 2:
    `./lib/types` -> `../../lib/types`). Bare package specifiers are untouched.
    Idempotency is not required (called once per copy).
    """
    if extra_depth <= 0:
        return tsx
    prefix = "../" * extra_depth

    def _sub(m: "re.Match[str]") -> str:
        spec = m.group(3)
        # Drop a leading `./` so a same-dir import re-anchors to a clean
        # `../../<rest>` instead of `../.././<rest>` (both resolve, but the
        # former is what the plan specifies and reads clearly).
        if spec.startswith("./"):
            spec = spec[2:]
        return f"{m.group(1)}{m.group(2)}{prefix}{spec}{m.group(2)}"

    return _RELATIVE_IMPORT_RE.sub(_sub, tsx)


def copy_shared_component_tsx(
    *,
    run_id: int,
    component: str,
    run_dir: Path,
    source_override: Path | None = None,
    error_cls: type[RuntimeError] = RemotionManifestError,
) -> Path:
    """Seed a per-run editable COPY of a SHARED component into the run dir.

    Copies the committed shared component (`Captions.tsx`) to
    ``runs/<video_id>/remotion/<component>.tsx`` so a run can patch it without
    touching ``shared/``. This is the canonical, run-dir-durable copy the change
    agent Edits; ``stage_run_component`` later stages it for the renderer. The
    change agent normally calls this ONCE, then Edits the copy in the run dir.
    Returns the destination path.
    """
    source = source_override or SHARED_COMPONENT_SOURCES.get(component)
    if source is None:
        raise error_cls(f"unknown shared component: {component!r}")
    if not source.exists() or source.stat().st_size == 0:
        raise error_cls(f"shared {component} source missing or empty: {source}")
    dest_dir = run_dir / "remotion"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{component}.tsx"
    dest.write_text(source.read_text(encoding="utf-8-sig"), encoding="utf-8")
    return dest


def stage_run_component(
    *,
    run_id: int,
    component: str,
    run_dir: Path,
) -> Path | None:
    """Stage a per-run component copy into the renderer's transient tree.

    If ``runs/<video_id>/remotion/<component>.tsx`` exists, copy it to
    ``REMOTION_SRC/runs/<run_id>/<component>.tsx`` (rewriting its relative
    imports for the deeper location) and return that staged path. Otherwise
    return None — the renderer then falls back to the shared component.

    The staged copy sits at ``runs/<run_id>/<component>.tsx`` (2 levels below
    ``REMOTION_SRC``), so its `./lib/...`-style imports are rewritten to
    `../../lib/...`. Shared components use no ``staticFile("assets/...")``, so no
    asset namespacing is applied here (unlike per-segment scenes).
    """
    canonical = run_dir / "remotion" / f"{component}.tsx"
    if not canonical.exists() or canonical.stat().st_size == 0:
        return None
    dest_dir = REMOTION_SRC / "runs" / str(run_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{component}.tsx"
    text = canonical.read_text(encoding="utf-8-sig")
    # runs/<run>/<component>.tsx is 2 dirs below src/ where the shared component
    # (and lib/) live, so climb 2 levels to re-anchor relative imports.
    text = _rewrite_relative_import_depth(text, extra_depth=2)
    dest.write_text(text, encoding="utf-8")
    return dest


def _copy_component_tsx(
    *,
    run_id: int,
    segment_id: int,
    source_tsx: Path,
    component_basename: str,
    namespace_assets: bool = False,
    error_cls: type[RuntimeError] = RemotionManifestError,
) -> Path:
    """Place an authored component TSX under remotion/src/runs/<run>/<seg>/.

    When `namespace_assets` is set, the component's `staticFile("assets/...")`
    paths are rewritten to the per-run namespace `assets/runs/<run_id>/...` so
    the render loads each run's own staged images. Returns the destination path.
    """
    if not source_tsx.exists() or source_tsx.stat().st_size == 0:
        raise error_cls(
            f"source {component_basename}.tsx missing or empty: {source_tsx}"
        )
    dest_dir = REMOTION_SRC / "runs" / str(run_id) / str(segment_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{component_basename}.tsx"
    text = source_tsx.read_text(encoding="utf-8-sig")
    if namespace_assets:
        text = _namespace_static_assets(text, str(run_id))
    dest.write_text(text, encoding="utf-8")
    return dest


def _entry_ts(
    *,
    comp_id: str,
    component_expr: str,
    frames: int,
    width: int,
    height: int,
    default_props: Any,
) -> str:
    """One COMPOSITIONS array entry as TS source."""
    return (
        "  {\n"
        f"    id: {_ts_string(comp_id)},\n"
        f"    component: {component_expr},\n"
        f"    durationInFrames: {frames},\n"
        f"    width: {width},\n"
        f"    height: {height},\n"
        f"    defaultProps: {_ts_string(default_props)},\n"
        "  }"
    )


def _write_manifest(
    *,
    run_id: int,
    fps: int,
    width: int,
    height: int,
    component_basename: str,
    entries: list[dict[str, Any]],
    captions_props: dict[str, Any] | None = None,
    out_path: Path | None = None,
    captions_component_import: str | None = None,
) -> Path:
    """Write one Compositions TS module — the shared core for every format.

    Args:
        run_id: numeric run id (used to namespace component imports).
        fps: frames per second.
        width/height: the format's canvas (also used for captions; a
            per-entry ``width``/``height`` overrides it for that entry).
        component_basename: per-segment component file/alias stem ("Segment"
            for long/faceless, "Scene" for short).
        entries: list of dicts with ``segment_id``, ``duration_sec`` and
            optional ``default_props``/``width``/``height``. The caller must
            have already copied each component TSX to
            ``remotion/src/runs/<run>/<segment>/<component_basename>.tsx``.
        captions_props: CaptionsProps payload, or None to skip the captions
            composition.
        out_path: destination .ts file. Defaults to the shared
            `Compositions.generated.ts`; isolated renders pass a unique
            per-render path so concurrent renders never clobber each other.
        captions_component_import: full TS import statement for Captions, or
            None to use the shared `./Captions` fallback. Callers pass a
            per-run copy import (e.g. `import { Captions } from
            "./runs/<run>/Captions";`) only when a per-run copy has been
            staged; None preserves today's shared behavior.

    Returns:
        Path to the generated .ts file.
    """
    out = out_path or (REMOTION_SRC / "Compositions.generated.ts")

    import_lines: list[str] = [
        "// AUTO-GENERATED by Python orchestrator. DO NOT EDIT BY HAND.",
        'import type React from "react";',
    ]
    if captions_props is not None:
        import_lines.append(
            captions_component_import
            or 'import { Captions } from "./Captions";'
        )
    body_entries: list[str] = []

    for entry in entries:
        seg_id = int(entry["segment_id"])
        frames = max(round(float(entry["duration_sec"]) * fps), 1)
        # Static import per component. Using underscored alias because TS
        # rejects digits-only identifiers (Scene1 is fine; 1 is not).
        alias = f"{component_basename}_{run_id}_{seg_id}"
        rel = f"./runs/{run_id}/{seg_id}/{component_basename}"
        import_lines.append(f'import {alias} from "{rel}";')
        body_entries.append(
            _entry_ts(
                comp_id=f"segment-{seg_id}",
                component_expr=alias,
                frames=frames,
                width=int(entry.get("width", width)),
                height=int(entry.get("height", height)),
                default_props=entry.get("default_props") or {},
            )
        )

    if captions_props is not None:
        if "durationSec" not in captions_props:
            # Strict on purpose: defaulting to 0 would silently emit a
            # zero-frame captions composition. Applies to every format that
            # routes through this core (long/faceless and short).
            raise ValueError(
                "captions_props is missing the required key 'durationSec' "
                "(captions composition duration in seconds)"
            )
        frames = round(float(captions_props["durationSec"]) * fps)
        body_entries.append(
            _entry_ts(
                comp_id="captions",
                component_expr="Captions",
                frames=frames,
                width=width,
                height=height,
                default_props=captions_props,
            )
        )

    body = (
        "\n".join(import_lines)
        + '\nimport type { CompositionEntry } from "./lib/types";\n'
        + "\nexport const COMPOSITIONS: ReadonlyArray<CompositionEntry & { component: React.FC<any> }> = [\n"
        + ",\n".join(body_entries)
        + "\n];\n"
    )
    out.write_text(body, encoding="utf-8")
    return out
