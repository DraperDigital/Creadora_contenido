"""Orchestration-level tests for pipeline.surgical_recut.

The heavy pieces (the cut_editor agent, the deterministic resurface subprocess,
and the whole short phase) are mocked; these tests pin the CONTROL FLOW — that a
cut change is applied PINPOINTED (edit the existing edl -> resurface -> re-derive
via the NO-FORCE short phase, invalidating only the scenes over the changed
moment) and NEVER via a full rebuild (`force=True` / `recut_and_rederive`).
"""
import pytest

import contenido_bionico.pipeline as pl
from contenido_bionico.shared.cut import surgical_recut as sr
import contenido_bionico.shared.cut.orchestrator as cut_orch
import contenido_bionico.short.animate.orchestrator as anim
import contenido_bionico.short.change.cut_editor as ced

_EDL_OLD = '{"source":"word_for_word","ranges":[{"source":"word_for_word","start":0.0,"end":5.0},{"source":"word_for_word","start":10.0,"end":15.0}]}'
_EDL_NEW = '{"source":"word_for_word","ranges":[{"source":"word_for_word","start":0.0,"end":5.0},{"source":"word_for_word","start":11.0,"end":15.0}]}'


def _mk_run(tmp_path, vid, *, with_plan=True):
    rd = tmp_path / vid
    (rd / "_intermediates").mkdir(parents=True)
    (rd / "_intermediates" / "edl_render.json").write_text(_EDL_OLD, encoding="utf-8")
    if with_plan:
        (rd / "requests").mkdir(parents=True)
        (rd / "requests" / "Scenes_Plan.json").write_text('{"video_id":"77","scenes":[]}', encoding="utf-8")
    return rd


def _forbid_full_rebuild(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("FORBIDDEN: a full rebuild path was called for a surgical recut")
    monkeypatch.setattr(pl, "recut_and_rederive", boom)
    monkeypatch.setattr(cut_orch, "recut", boom)


def test_surgical_recut_is_pinpointed_not_a_rebuild(monkeypatch, tmp_path):
    vid = "77_short"
    rd = _mk_run(tmp_path, vid)
    monkeypatch.setattr(pl, "RUNS_DIR", tmp_path)
    _forbid_full_rebuild(monkeypatch)

    seen = {}

    def fake_cut_editor(video_id, notes, run_dir):
        seen["cut_editor"] = (video_id, notes)
        (rd / "_intermediates" / "edl_render.json").write_text(_EDL_NEW, encoding="utf-8")
    monkeypatch.setattr(ced, "run_cut_editor", fake_cut_editor)
    monkeypatch.setattr(cut_orch, "resurface_from_edl", lambda v, **k: seen.setdefault("resurface", v))
    monkeypatch.setattr(sr, "remap_scenes_plan", lambda p, o, n: ([1], [2], [3]))  # reused, affected, dropped
    invalidated = []
    monkeypatch.setattr(anim, "_invalidate_one_scene_render_artifacts", lambda v, s: invalidated.append(s))

    phase = {}
    monkeypatch.setattr(pl, "run_short_phase", lambda video_id, **kw: (phase.update(kw, video_id=video_id) or (tmp_path / "final.mp4")))

    out = pl.surgical_recut(vid, "usa la ultima toma de la frase de cierre",
                            anim_opts={"anim_quality": "high", "no_music": False})

    assert seen["cut_editor"] == (vid, "usa la ultima toma de la frase de cierre")
    assert seen["resurface"] == vid
    assert invalidated == [2]                    # ONLY the 'affected' scene; reused/dropped untouched
    assert phase["force"] is False               # <-- pinpointed, not a rebuild
    assert phase["build_extras"] is False
    assert phase["anim_quality"] == "high"
    assert out == tmp_path / "final.mp4"


def test_surgical_recut_raises_when_editor_made_no_change(monkeypatch, tmp_path):
    vid = "78_short"
    _mk_run(tmp_path, vid)
    monkeypatch.setattr(pl, "RUNS_DIR", tmp_path)
    _forbid_full_rebuild(monkeypatch)
    monkeypatch.setattr(ced, "run_cut_editor", lambda v, n, r: None)  # leaves edl unchanged
    monkeypatch.setattr(cut_orch, "resurface_from_edl", lambda v, **k: pytest.fail("resurface must not run"))
    monkeypatch.setattr(pl, "run_short_phase", lambda *a, **k: pytest.fail("phase must not run"))
    with pytest.raises(SystemExit):
        pl.surgical_recut(vid, "algo")


def test_surgical_recut_raises_on_missing_edl(monkeypatch, tmp_path):
    vid = "79_short"
    (tmp_path / vid / "_intermediates").mkdir(parents=True)  # no edl_render.json
    monkeypatch.setattr(pl, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(ced, "run_cut_editor", lambda v, n, r: pytest.fail("editor must not run without an edl"))
    with pytest.raises(SystemExit):
        pl.surgical_recut(vid, "algo")


def test_surgical_recut_rejects_invalid_edited_ranges(monkeypatch, tmp_path):
    vid = "80_short"
    rd = _mk_run(tmp_path, vid)
    monkeypatch.setattr(pl, "RUNS_DIR", tmp_path)
    _forbid_full_rebuild(monkeypatch)
    # editor writes overlapping ranges -> must be rejected before any render
    monkeypatch.setattr(ced, "run_cut_editor", lambda v, n, r: (rd / "_intermediates" / "edl_render.json").write_text(
        '{"source":"word_for_word","ranges":[{"start":0.0,"end":6.0},{"start":5.0,"end":9.0}]}', encoding="utf-8"))
    monkeypatch.setattr(cut_orch, "resurface_from_edl", lambda v, **k: pytest.fail("resurface must not run on invalid edl"))
    monkeypatch.setattr(pl, "run_short_phase", lambda *a, **k: pytest.fail("phase must not run on invalid edl"))
    with pytest.raises(SystemExit):
        pl.surgical_recut(vid, "algo")
