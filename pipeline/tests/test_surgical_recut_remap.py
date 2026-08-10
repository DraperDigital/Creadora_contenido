"""Unit tests for the pure time-remap helpers in
``contenido_bionico.shared.cut.surgical_recut``.

Fixtures use REAL run-12 EDL numbers. ``old_ranges`` is run-12's kept SOURCE
ranges; ``new_ranges`` is the same edit with only the LAST range (the closing
take, source 37.02..43.579) swapped for a different source region
(45.0..46.679). Every earlier range is byte-for-byte identical, so the whole cut
timeline is identical up to the final range boundary -- which is what lets us
assert identity before the edit and disappearance of the swapped-out region.
"""

import json

from contenido_bionico.shared.cut import surgical_recut as sr


OLD_RANGES = [
    (0.179, 1.859),
    (2.019, 3.779),
    (7.419, 10.039),
    (11.88, 13.919),
    (17.719, 24.239),
    (24.379, 26.199),
    (37.02, 43.579),
]
# Same edit, but the closing take (last range) is replaced. All earlier ranges
# are identical, so cut-times before the final range are unchanged.
NEW_RANGES = OLD_RANGES[:-1] + [(45.0, 46.679)]


def test_cut_to_source_start():
    # CUT-time 0 maps to the first kept source second.
    assert sr.cut_to_source(OLD_RANGES, 0.0) == 0.179


def test_identity_before_edit():
    # Range index 1 occupies CUT-time [1.680, 3.440); pick a time inside it.
    # Since NEW_RANGES is identical up to the final range, remapping is identity.
    t = 2.5
    out = sr.remap_cut_time(OLD_RANGES, NEW_RANGES, t)
    assert out is not None
    assert abs(out - t) < 1e-3


def test_swapped_out_source_is_gone():
    # Source second 40.0 lived inside the OLD closing take (37.02..43.579), which
    # the NEW edit swapped out entirely -> no cut-time in the new timeline.
    assert sr.source_to_cut(NEW_RANGES, 40.0) is None


def _write_plan(tmp_path, scenes):
    plan = {"video_id": "run-12", "scenes": scenes}
    p = tmp_path / "Scenes_Plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_remap_scenes_plan_before_and_over_edit(tmp_path):
    # Scene A lies wholly before the edit: CUT 3.0..4.0 sits inside range index 2
    # ([3.440, 6.060)), untouched by the swap -> clean remap, ~unchanged times.
    scene_a = {"segment_id": 1, "time_start": 3.0, "time_end": 4.0}
    # Scene B's SOURCE span is the swapped-out closing region: CUT 17.419..20.419
    # maps through OLD ranges into source 38.0..41.0, all inside 37.02..43.579.
    scene_b = {"segment_id": 2, "time_start": 17.419, "time_end": 20.419}

    plan_path = _write_plan(tmp_path, [scene_a, scene_b])

    remapped, affected, dropped = sr.remap_scenes_plan(
        plan_path, OLD_RANGES, NEW_RANGES
    )

    # Scene A: cleanly remapped, times essentially unchanged.
    assert 1 in remapped
    assert 1 not in affected
    assert 1 not in dropped

    reloaded = json.loads(plan_path.read_text(encoding="utf-8"))
    a_out = next(s for s in reloaded["scenes"] if s["segment_id"] == 1)
    assert abs(a_out["time_start"] - 3.0) < 1e-3
    assert abs(a_out["time_end"] - 4.0) < 1e-3

    # Scene B sits over the edited region -> affected OR dropped, never remapped.
    assert 2 not in remapped
    assert (2 in affected) or (2 in dropped)


def test_interior_edit_marks_containing_scene_affected(tmp_path):
    # Regression for the word-sync-drift bug: a scene that CONTAINS a small
    # interior deletion (drop a ~150ms false-start syllable) has BOTH endpoints
    # still mapping, but its words changed -> it MUST be 'affected' (re-authored),
    # never 'remapped' (which would reuse a stale render over edited audio). A
    # scene entirely AFTER the edit shifts rigidly and IS reused.
    old = [(0.0, 30.0)]
    new = [(0.0, 5.0), (5.15, 30.0)]                 # dropped source [5.0, 5.15]
    scene_over = {"segment_id": 1, "time_start": 2.0, "time_end": 12.0}   # contains the edit
    scene_after = {"segment_id": 2, "time_start": 20.0, "time_end": 25.0}  # entirely after
    plan_path = _write_plan(tmp_path, [scene_over, scene_after])

    remapped, affected, dropped = sr.remap_scenes_plan(plan_path, old, new)

    assert 1 in affected and 1 not in remapped        # containing scene re-authors
    assert 2 in remapped and 2 not in affected        # after-edit scene reused (rigid shift)

    reloaded = json.loads(plan_path.read_text(encoding="utf-8"))
    after = next(s for s in reloaded["scenes"] if s["segment_id"] == 2)
    assert abs(after["time_start"] - 19.85) < 1e-3    # shifted back by the 0.15s drop
    assert abs(after["time_end"] - 24.85) < 1e-3
