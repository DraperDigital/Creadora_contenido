"""Pure, dependency-free time-remap helpers for a "surgical recut".

Motivation
==========
The cut phase de-silences a working video and then keeps a set of SOURCE-timeline
ranges. The final CUT timeline is the concatenation of those kept ranges, starting
at 0. Everything downstream (scene plans, camera, captions) is expressed in
CUT-timeline seconds.

When an editor re-cuts a video, the kept ranges change: a range may be shifted,
lengthened, shortened, dropped, or replaced (a "closing take" swapped for a
different source region). A CUT-time that was valid against the OLD edit no longer
lines up with the NEW edit, because the concatenation offsets shift and some
source regions disappear entirely.

Model
=====
An EDL is an ordered, non-overlapping list of kept SOURCE ranges::

    ranges = [(start_0, end_0), (start_1, end_1), ...]   # SOURCE seconds

The CUT timeline lays these ranges end-to-end starting at 0. If we let
``dur_i = end_i - start_i`` and ``offset_i = sum(dur_j for j < i)``, then:

* a CUT-time ``t`` in ``[offset_i, offset_i + dur_i)`` corresponds to SOURCE-time
  ``start_i + (t - offset_i)``   (``cut_to_source``);
* a SOURCE-time ``s`` in ``[start_i, end_i]`` corresponds to CUT-time
  ``offset_i + (s - start_i)``   (``source_to_cut``); a SOURCE-time that falls in
  no kept range was cut out and has no CUT-time (``None``).

Remapping a CUT-time from the OLD edit to the NEW edit is the composition:
``source = cut_to_source(old_ranges, t)`` then
``new_cut = source_to_cut(new_ranges, source)``. The intermediate SOURCE-time is
edit-independent, which is what makes it the stable pivot between the two
timelines (``remap_cut_time``).

This module is intentionally pure: stdlib only (``json``, ``pathlib``, ``math``),
no imports from ``contenido_bionico``, deterministic, and side-effect-free except
for ``remap_scenes_plan`` which rewrites the plan file it is given.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

# Tolerance for treating a source-time as "inside" a kept range and for
# collapsing near-zero remapped spans. Chosen well below frame/sample precision.
EPS = 1e-6

# Number of evenly-spaced samples used when re-anchoring a scene that straddles
# the edited region.
_REANCHOR_SAMPLES = 24

# A scene may be REUSED (classified `remapped`) only when it moved RIGIDLY: every
# point in its span shifts by the SAME cut-time delta and no interior source was
# cut. Tolerance is well under one frame (~33ms @30fps) so a stale render is never
# reused over edited audio. `_RIGID_SAMPLES` interior points are checked so an
# edit strictly INSIDE the span (both endpoints still map) is still caught.
_RIGID_SHIFT_TOL = 1e-3
_RIGID_SAMPLES = 8


def _rigidly_shifted(old_ranges, new_ranges, a, b, new_a, new_b) -> bool:
    """True only if the scene's whole CUT span shifts by a CONSTANT delta from the
    OLD to the NEW edit -- i.e. nothing inside ``[a, b]`` was cut, added, or
    re-timed. That is the ONLY case where the scene's cached render is still valid
    (same words, same internal timing, merely re-placed on the new timeline).

    An INTERIOR edit -- dropping a false-start syllable, tightening a sub-frame
    silence, loosening a clipped word -- leaves both endpoints mapping but shifts
    the end relative to the start (and/or removes an interior sample), so the words
    the scene animates change. Such a scene must re-author, not reuse a stale
    render, even though ``new_a``/``new_b`` are both non-None. This check is what
    separates "moved" (reuse) from "content changed" (re-author).
    """
    base = new_a - a
    if abs((new_b - b) - base) > _RIGID_SHIFT_TOL:
        return False
    span = b - a
    if span <= 0.0:
        return True
    for i in range(1, _RIGID_SAMPLES):
        t = a + span * (i / _RIGID_SAMPLES)
        t2 = remap_cut_time(old_ranges, new_ranges, t)
        if t2 is None or abs((t2 - t) - base) > _RIGID_SHIFT_TOL:
            return False
    return True


def load_ranges(edl_path) -> list[tuple[float, float]]:
    """Read ``edl_render.json`` and return kept ranges as ``[(start, end), ...]``.

    Only entries with ``end > start`` are kept, preserving file order. The file is
    read as ``utf-8-sig`` so a UTF-8 BOM (if present) is stripped transparently.
    """
    path = Path(edl_path)
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    ranges: list[tuple[float, float]] = []
    for entry in data.get("ranges", []):
        start = float(entry["start"])
        end = float(entry["end"])
        if end > start:
            ranges.append((start, end))
    return ranges


def cut_to_source(ranges, t_cut) -> float:
    """Map a CUT-timeline second ``t_cut`` to its SOURCE-timeline second.

    Walks the cumulative kept durations to find the range that contains ``t_cut``.
    With no ranges the identity value is returned. ``t_cut`` is clamped to
    ``[0, total_cut_duration]``; a value at or past the end maps to the last
    range's end.
    """
    if not ranges:
        return t_cut

    if t_cut <= 0.0:
        return ranges[0][0]

    offset = 0.0
    for start, end in ranges:
        dur = end - start
        # t_cut lands within this range's span on the cut timeline.
        if t_cut <= offset + dur:
            return start + (t_cut - offset)
        offset += dur

    # Past the end of the whole cut timeline -> last kept source second.
    return ranges[-1][1]


def source_to_cut(ranges, t_src) -> float | None:
    """Map a SOURCE-timeline second ``t_src`` to its CUT-timeline second.

    Returns the cut-time if ``t_src`` falls within ``[start - EPS, end + EPS]`` of
    some kept range; otherwise ``None`` (the source-time was cut out). The first
    matching range wins (ranges are ordered and non-overlapping).
    """
    offset = 0.0
    for start, end in ranges:
        if (start - EPS) <= t_src <= (end + EPS):
            # Clamp within the range so tolerance slack cannot push the result
            # slightly out of the range's cut-time span.
            local = t_src - start
            if local < 0.0:
                local = 0.0
            elif local > (end - start):
                local = end - start
            return offset + local
        offset += end - start
    return None


def remap_cut_time(old_ranges, new_ranges, t) -> float | None:
    """Remap a CUT-time from the OLD edit to the NEW edit.

    Composition: ``cut_to_source(old_ranges, t)`` then
    ``source_to_cut(new_ranges, .)``. Returns ``None`` if the underlying
    source-time no longer survives in ``new_ranges``.
    """
    src = cut_to_source(old_ranges, t)
    return source_to_cut(new_ranges, src)


def _is_number(value) -> bool:
    """True for a real, finite int/float (rejects bool, NaN, inf, non-numbers)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def remap_scenes_plan(plan_path, old_ranges, new_ranges):
    """Remap every scene in a ``Scenes_Plan.json`` from the OLD to the NEW edit.

    For each scene ``[time_start, time_end]`` (CUT-time, OLD edit):

    * If both endpoints remap, the remapped ``end > start``, AND the scene moved
      RIGIDLY (constant shift, no interior source cut -- see ``_rigidly_shifted``):
      update the scene's ``time_start`` / ``time_end`` in place (rounded to 3 dp),
      record the ``segment_id`` in ``remapped``, keep the scene. This is the
      pure-reuse case: same content, merely re-placed.
    * Otherwise the scene overlaps / contains the edited region (its content
      changed): attempt a best-effort re-anchor by sampling ~24 points evenly
      across the scene's SOURCE span and keeping the survivors under the NEW edit.
      If any survive with a non-trivial span, set ``time_start`` / ``time_end`` to
      that ``(min, max)`` (rounded), record in ``affected``, keep the scene. If
      none survive, record in ``dropped`` and REMOVE the scene. An ``affected``
      scene has its render invalidated by the caller so it re-authors against the
      new transcript slice -- never reusing a stale animation over edited audio.
    * Malformed scenes (missing / non-numeric ``segment_id`` /
      ``time_start`` / ``time_end``) are left untouched and kept.

    The file is rewritten with ``json.dumps(..., ensure_ascii=False, indent=2)``.
    Returns ``(remapped_ids, affected_ids, dropped_ids)``.
    """
    path = Path(plan_path)
    with path.open("r", encoding="utf-8-sig") as fh:
        plan = json.load(fh)

    scenes = plan.get("scenes", [])

    remapped_ids: list[int] = []
    affected_ids: list[int] = []
    dropped_ids: list[int] = []
    kept_scenes: list = []

    for scene in scenes:
        # Guard: malformed scene entries are left untouched and kept.
        if not isinstance(scene, dict):
            kept_scenes.append(scene)
            continue
        seg = scene.get("segment_id")
        a = scene.get("time_start")
        b = scene.get("time_end")
        if not (_is_number(seg) and _is_number(a) and _is_number(b)):
            kept_scenes.append(scene)
            continue

        seg_id = int(seg)
        a = float(a)
        b = float(b)

        # Pure reuse ONLY when the scene moved rigidly (constant shift, nothing
        # cut inside it). Endpoints mapping is necessary but NOT sufficient: an
        # edit strictly inside the span leaves both endpoints mapping yet changes
        # the words -> it must fall through to `affected` and re-author.
        new_a = remap_cut_time(old_ranges, new_ranges, a)
        new_b = remap_cut_time(old_ranges, new_ranges, b)
        if (
            new_a is not None
            and new_b is not None
            and (new_b - new_a) > EPS
            and _rigidly_shifted(old_ranges, new_ranges, a, b, new_a, new_b)
        ):
            scene["time_start"] = round(new_a, 3)
            scene["time_end"] = round(new_b, 3)
            remapped_ids.append(seg_id)
            kept_scenes.append(scene)
            continue

        # Scene straddles/contains the edited region: best-effort re-anchor by sampling
        # the scene's SOURCE span and collecting survivors under the NEW edit.
        src_a = cut_to_source(old_ranges, a)
        src_b = cut_to_source(old_ranges, b)
        survivors: list[float] = []
        for i in range(_REANCHOR_SAMPLES):
            frac = i / (_REANCHOR_SAMPLES - 1) if _REANCHOR_SAMPLES > 1 else 0.0
            src = src_a + (src_b - src_a) * frac
            cut = source_to_cut(new_ranges, src)
            if cut is not None:
                survivors.append(cut)

        if survivors and (max(survivors) - min(survivors)) > EPS:
            scene["time_start"] = round(min(survivors), 3)
            scene["time_end"] = round(max(survivors), 3)
            affected_ids.append(seg_id)
            kept_scenes.append(scene)
        else:
            dropped_ids.append(seg_id)
            # Scene removed: not appended to kept_scenes.

    plan["scenes"] = kept_scenes
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(plan, ensure_ascii=False, indent=2))

    return remapped_ids, affected_ids, dropped_ids
