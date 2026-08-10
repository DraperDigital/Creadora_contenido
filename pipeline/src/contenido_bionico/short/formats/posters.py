"""Poster producers (category `posters`).

Eight deliverables = four LAYOUTS x two backgrounds. A layout is purely an
element distribution (no theme, no genre, no "purpose"); all eight share ONE
neutral content distillation (`poster_content.content`) so the text is always
tied to the video, never to an invented rationale. The two backgrounds:
  - solid: a designed background the comp draws.
  - foto:  the real photo plate from `broll_library/cutouts/` (the `*back`
           image), with the matching `*front` cutout composited on top — never
           an invented background.

Every poster obeys the layering rule (enforced by each comp via `PosterFrame`):
top-of-frame text renders behind the subject (the face stays clean), bottom text
in front (it may cover the body). 1080x1350 stills.

Best-effort: no content -> the poster skips; the foto variant additionally skips
when there is no usable photo plate.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from contenido_bionico.short.formats import broll, poster_content
from contenido_bionico.shared.remotion_manifest_formats import render_format_stills
from contenido_bionico.shared.remotion_runtime import REMOTION_PUBLIC

WIDTH = 1080
HEIGHT = 1350

# (layout number, component file, composition id base)
_LAYOUTS = {1: "PosterLayout1", 2: "PosterLayout2", 3: "PosterLayout3", 4: "PosterLayout4"}


# --- staging ------------------------------------------------------------------

def _asset_name(src: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(src).stem)[:32].strip("_") or "img"
    digest = hashlib.sha1(str(Path(src).resolve()).encode("utf-8")).hexdigest()[:8]
    return f"poster_{stem}_{digest}{Path(src).suffix.lower()}"


def _stage(ctx, src: Path) -> str | None:
    """Copy `src` into the run's remotion assets; return its staticFile path."""
    if src is None or not Path(src).exists():
        return None
    dest_dir = REMOTION_PUBLIC / "assets" / "runs" / str(ctx.video_id) / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _asset_name(src)
    dest = dest_dir / name
    data = Path(src).read_bytes()
    if not dest.exists() or dest.stat().st_size != len(data):
        dest.write_bytes(data)
    return f"assets/runs/{ctx.video_id}/broll/{name}"


def _solid_subject(ctx, index: int = 0) -> Path | None:
    """The `index`-th distinct foreground cutout for a SOLID poster, composited
    over the layout's designed background. `index` (0-based) varies the person per
    layout so sibling solid posters never reuse the same cutout."""
    return broll.select_subject_cutout_for(ctx, index)


def _foto_pair(ctx, index: int = 0) -> tuple[Path, Path] | None:
    """`(plate, foreground)` for the foto variant: the `index`-th complete matched
    cutout pair from `broll_library/cutouts/` (an opaque `background_N` plate + its
    transparent `foreground_N` cutout). `index` (0-based) varies the pair per
    layout so sibling foto posters use a different combo. Never generated from
    library photos — a run without a complete pair skips the foto variant. None
    when there is no pair whose plate AND cutout are both present."""
    pair = broll.select_cutout_pair_for(ctx, index)
    if pair is None or pair[0] is None:
        return None
    return pair[0], pair[1]


def _render(ctx, component_file: str, composition_id: str, props: dict, filename: str) -> list[Path]:
    if ctx.out_dir is None:
        return []
    out = Path(ctx.out_dir) / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    render_format_stills(
        composition_id=composition_id, component_file=component_file,
        width=WIDTH, height=HEIGHT, duration_frames=1,
        props=props, out_pngs=[out], frames=[0],
    )
    return [out]


def _produce(ctx, layout: int, *, foto: bool) -> list[Path]:
    content = poster_content.content(ctx)
    if not content or not content.get("headline"):
        return []
    palette = ctx.palette()
    comp = _LAYOUTS[layout]
    # Each layout draws a DISTINCT cutout (index = layout - 1) so sibling posters
    # never reuse the same person/pair even when the text is identical.
    if foto:
        pair = _foto_pair(ctx, layout - 1)
        if pair is None:
            return []  # foto needs a real plate; skip cleanly
        plate, fg = pair
        props = {"content": content, "mode": "foto", "palette": palette,
                 "bgSrc": _stage(ctx, plate), "subjectSrc": _stage(ctx, fg)}
        return _render(ctx, f"formats/{comp}", f"poster-layout-{layout}-foto", props, f"poster_{layout}_foto.png")
    props = {"content": content, "mode": "solid", "palette": palette,
             "subjectSrc": _stage(ctx, _solid_subject(ctx, layout - 1))}
    return _render(ctx, f"formats/{comp}", f"poster-layout-{layout}", props, f"poster_{layout}.png")


# --- the 8 producers (4 layouts x solid/foto) ---------------------------------

def produce_1(ctx):        return _produce(ctx, 1, foto=False)
def produce_1_foto(ctx):   return _produce(ctx, 1, foto=True)
def produce_2(ctx):        return _produce(ctx, 2, foto=False)
def produce_2_foto(ctx):   return _produce(ctx, 2, foto=True)
def produce_3(ctx):        return _produce(ctx, 3, foto=False)
def produce_3_foto(ctx):   return _produce(ctx, 3, foto=True)
def produce_4(ctx):        return _produce(ctx, 4, foto=False)
def produce_4_foto(ctx):   return _produce(ctx, 4, foto=True)
