"""Remotion render wrapper for animation segments.

Two render paths:

  - Default (alpha): VP9 WebM with yuva420p + PNG frames. Required for
    transparent overlays (captions, /animar overlay scenes).
  - Fast opaque (``fast_opaque=True``): H.264 MP4 with yuv420p + JPEG frames.
    Explicit opt-in for full-bleed opaque renders (faceless segments) where
    the alpha channel is pure waste — the assembly flattens to yuv420p
    anyway. H.264 cannot live in a WebM container, so this path requires an
    ``.mp4`` output file.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Callable

from contenido_bionico.shared import remotion_runtime
from contenido_bionico.shared.ffmpeg import NO_WINDOW


REMOTION_DIR = Path(__file__).resolve().parent / "remotion"
REMOTION_SRC = REMOTION_DIR / "src"
RENDER_TIMEOUT_SECONDS = 30 * 60

# Per-render entrypoint template. Each isolated render writes its own
# index.<token>.tsx that imports its own Compositions.<token>.ts, so concurrent
# renders (within a run AND across runs/processes) never share or clobber the
# composition registry. Mirrors the static Root.tsx render loop.
_ISOLATED_ENTRY_TEMPLATE = """\
// AUTO-GENERATED per-render entrypoint. Transient — safe to delete.
import {{ registerRoot }} from "remotion";
import React from "react";
import {{ Composition }} from "remotion";
import {{ FPS }} from "./lib/config";
import {{ COMPOSITIONS }} from "./{module}";

const Root: React.FC = () => (
  <>
    {{COMPOSITIONS.map(({{ id, component, durationInFrames, width, height, defaultProps }}) => (
      <Composition
        key={{id}}
        id={{id}}
        component={{component}}
        durationInFrames={{durationInFrames}}
        fps={{FPS}}
        width={{width}}
        height={{height}}
        defaultProps={{defaultProps as any}}
      />
    ))}}
  </>
);

registerRoot(Root);
"""


class RemotionRenderError(RuntimeError):
    pass


def _ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
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
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RemotionRenderError(f"ffprobe duration failed: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def _ffprobe_has_alpha(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RemotionRenderError(f"ffprobe streams failed: {proc.stderr.strip()}")
    data = proc.stdout.lower()
    return "tag:alpha_mode=1" in data or "alpha_mode=1" in data


def render_composition(
    *,
    composition_id: str,
    out_webm: Path,
    expected_duration: float | None = None,
    duration_tolerance: float = 0.2,
    require_alpha: bool = True,
    entry: str = "src/index.ts",
    fast_opaque: bool = False,
) -> Path:
    """Render one generated Remotion composition.

    Default path: VP9 WebM (yuva420p, PNG frames) — `out_webm` is a `.webm`
    file that can carry an alpha channel.

    `fast_opaque=True` is an EXPLICIT opt-in for opaque full-bleed renders:
    H.264 MP4 (yuv420p, JPEG frames), much cheaper than PNG+VP9-alpha. H.264
    cannot live in a WebM container, so `out_webm` must then be an `.mp4`
    file, and `require_alpha` must be False.

    `entry` is the Remotion entrypoint (relative to the Remotion project root).
    Defaults to the shared static entry; isolated renders pass a per-render
    `src/index.<token>.tsx` so concurrent renders never share a bundle root.
    """
    if fast_opaque:
        if require_alpha:
            raise RemotionRenderError(
                "fast_opaque renders cannot require alpha (H.264 yuv420p has "
                f"no alpha channel): comp={composition_id}"
            )
        if out_webm.suffix.lower() != ".mp4":
            raise RemotionRenderError(
                "fast_opaque renders use H.264, which requires an .mp4 "
                f"container; got: {out_webm}"
            )
    out_webm.parent.mkdir(parents=True, exist_ok=True)
    if out_webm.exists():
        out_webm.unlink()

    remotion_bin = remotion_runtime.remotion_bin()
    if not remotion_bin.exists():
        raise RemotionRenderError(
            "Remotion CLI is not installed at the repo root. "
            f"Expected: {remotion_bin}. Run `contenido-bionico setup` first."
        )

    if fast_opaque:
        # Opaque fast path: H.264 + JPEG frames. No alpha plane to encode and
        # no per-frame PNG cost — the cheapest render Remotion offers.
        codec_args = ["--codec=h264", "--pixel-format=yuv420p", "--image-format=jpeg"]
    else:
        # Alpha path (default): VP9 needs PNG frames to keep alpha.
        # Highest-quality alpha overlay: PNG source frames + near-lossless VP9
        # (--crf=1) to minimize encode artifacts on sharp text edges.
        # (VP9 only supports yuva420p or yuva444p10le for alpha; libvpx encodes
        # the alpha plane at 4:2:0 regardless.)
        codec_args = [
            "--codec=vp9",
            "--pixel-format=yuva444p10le",
            "--image-format=png",
            "--crf=1",
        ]
    cmd = [
        str(remotion_bin),
        "render",
        entry,
        composition_id,
        str(out_webm),
        *codec_args,
        # Pin the public dir deterministically. Remotion otherwise auto-detects
        # its root from the nearest package.json (the package root), defaulting
        # publicDir to contenido-bionico/public (which does not exist) instead of
        # shared/remotion/public, the Remotion static asset root -> staticFile 404.
        f"--public-dir={REMOTION_DIR / 'public'}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REMOTION_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RENDER_TIMEOUT_SECONDS,
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0 or not out_webm.exists() or out_webm.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        raise RemotionRenderError(
            f"remotion render failed (exit {proc.returncode}) comp={composition_id}:\n{tail}"
        )

    if require_alpha and not _ffprobe_has_alpha(out_webm):
        raise RemotionRenderError(f"rendered webm has no alpha channel: {out_webm}")

    if expected_duration is not None:
        actual = _ffprobe_duration(out_webm)
        if abs(actual - expected_duration) > duration_tolerance:
            raise RemotionRenderError(
                f"rendered duration {actual:.3f}s drifts from expected "
                f"{expected_duration:.3f}s by more than {duration_tolerance:.2f}s "
                f"(comp={composition_id})"
            )

    return out_webm


def render_isolated_composition(
    *,
    write_compositions: Callable[[Path], object],
    composition_id: str,
    out_webm: Path,
    expected_duration: float | None = None,
    duration_tolerance: float = 0.2,
    require_alpha: bool = True,
    fast_opaque: bool = False,
) -> Path:
    """Render a composition from a per-render isolated entrypoint.

    `write_compositions` is a callback that writes a Compositions TS module to
    the path it is given (typically a thin wrapper around
    `remotion_manifest_*.write_compositions_ts(..., out_path=p)`).

    Each call generates a unique `Compositions.<token>.ts` + `index.<token>.tsx`
    pair inside the Remotion src tree, renders `composition_id` from that
    private entrypoint, then deletes the pair. Because nothing is shared, any
    number of renders can run concurrently — multiple scenes within one run and
    multiple runs across separate processes — without clobbering each other.
    The per-run `runs/<run_id>/<seg>/Scene.tsx` files are already namespaced, so
    the registry file was the only shared mutable state.
    """
    token = uuid.uuid4().hex[:12]
    comp_module = f"Compositions.{token}"
    comp_path = REMOTION_SRC / f"{comp_module}.ts"
    entry_path = REMOTION_SRC / f"index.{token}.tsx"
    REMOTION_SRC.mkdir(parents=True, exist_ok=True)
    write_compositions(comp_path)
    entry_path.write_text(
        _ISOLATED_ENTRY_TEMPLATE.format(module=comp_module), encoding="utf-8"
    )
    try:
        return render_composition(
            composition_id=composition_id,
            out_webm=out_webm,
            expected_duration=expected_duration,
            duration_tolerance=duration_tolerance,
            require_alpha=require_alpha,
            entry=f"src/{entry_path.name}",
            fast_opaque=fast_opaque,
        )
    finally:
        for transient in (entry_path, comp_path):
            try:
                transient.unlink()
            except FileNotFoundError:
                pass


def render_still(
    *,
    composition_id: str,
    out_png: Path,
    frame: int = 0,
    entry: str = "src/index.ts",
) -> Path:
    """Render ONE frame of a composition to a PNG still (opaque image).

    Used by the carousel: each slide is a separate frame of the same
    composition, rendered to its own PNG via `remotion still --frame=N`. The
    quote post uses it too (a single-frame `quote-post` composition). The
    `--public-dir` flag points Remotion at the bundled assets (the quote-post
    template + font) so `staticFile(...)` resolves during the still render.
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if out_png.exists():
        out_png.unlink()

    remotion_bin = remotion_runtime.remotion_bin()
    if not remotion_bin.exists():
        raise RemotionRenderError(
            "Remotion CLI is not installed at the repo root. "
            f"Expected: {remotion_bin}. Run `contenido-bionico setup` first."
        )

    cmd = [
        str(remotion_bin),
        "still",
        entry,
        composition_id,
        str(out_png),
        f"--frame={frame}",
        "--image-format=png",
        f"--public-dir={REMOTION_DIR / 'public'}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REMOTION_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RENDER_TIMEOUT_SECONDS,
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0 or not out_png.exists() or out_png.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        raise RemotionRenderError(
            f"remotion still failed (exit {proc.returncode}) "
            f"comp={composition_id} frame={frame}:\n{tail}"
        )
    return out_png


def render_isolated_still(
    *,
    write_compositions: Callable[[Path], object],
    composition_id: str,
    out_png: Path,
    frame: int = 0,
) -> Path:
    """Render frame `frame` of `composition_id` to ONE PNG from a private entry.

    Like `render_isolated_composition` but a single still. Writes a per-render
    `index.<token>.tsx` + `Compositions.<token>.ts` pair so it never clobbers a
    concurrent render, then deletes the pair. Used by the quote module: one
    `quote-post` composition (its props = a single quote) rendered to a lossless
    PNG.
    """
    token = uuid.uuid4().hex[:12]
    comp_module = f"Compositions.{token}"
    comp_path = REMOTION_SRC / f"{comp_module}.ts"
    entry_path = REMOTION_SRC / f"index.{token}.tsx"
    REMOTION_SRC.mkdir(parents=True, exist_ok=True)
    write_compositions(comp_path)
    entry_path.write_text(
        _ISOLATED_ENTRY_TEMPLATE.format(module=comp_module), encoding="utf-8"
    )
    try:
        return render_still(
            composition_id=composition_id,
            out_png=out_png,
            frame=frame,
            entry=f"src/{entry_path.name}",
        )
    finally:
        for transient in (entry_path, comp_path):
            try:
                transient.unlink()
            except FileNotFoundError:
                pass


def render_isolated_stills(
    *,
    write_compositions: Callable[[Path], object],
    composition_id: str,
    out_pngs: list[Path],
    frames: list[int] | None = None,
) -> list[Path]:
    """Render frames of `composition_id` to PNG stills (one per output path).

    Mirrors `render_isolated_composition` but produces a PNG per frame instead of
    a video. By default `out_pngs[i]` receives the contiguous frame `i`
    (0..N-1, the historical behavior); pass `frames` to sample scattered frame
    indices instead (`out_pngs[i]` then receives frame `frames[i]`). Writes a
    private per-render `index.<token>.tsx` + `Compositions.<token>.ts` pair so
    it never clobbers a concurrent render, then deletes the pair when done.
    """
    if not out_pngs:
        raise RemotionRenderError("render_isolated_stills: no output paths given")
    if frames is not None and len(frames) != len(out_pngs):
        raise RemotionRenderError(
            "render_isolated_stills: frames and out_pngs must have the same "
            f"length (got {len(frames)} frames for {len(out_pngs)} outputs)"
        )
    token = uuid.uuid4().hex[:12]
    comp_module = f"Compositions.{token}"
    comp_path = REMOTION_SRC / f"{comp_module}.ts"
    entry_path = REMOTION_SRC / f"index.{token}.tsx"
    REMOTION_SRC.mkdir(parents=True, exist_ok=True)
    write_compositions(comp_path)
    entry_path.write_text(
        _ISOLATED_ENTRY_TEMPLATE.format(module=comp_module), encoding="utf-8"
    )
    try:
        for i, out_png in enumerate(out_pngs):
            render_still(
                composition_id=composition_id,
                out_png=out_png,
                frame=frames[i] if frames is not None else i,
                entry=f"src/{entry_path.name}",
            )
        return out_pngs
    finally:
        for transient in (entry_path, comp_path):
            try:
                transient.unlink()
            except FileNotFoundError:
                pass
