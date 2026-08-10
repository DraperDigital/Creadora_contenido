"""Unit tests for the split-screen bottom-band authoring loop.

The agent CLI (`call_agent`), the Remotion render (`render_format_video`), the
component staging (`_copy_component_tsx`) and the ffmpeg calls (`subprocess.run`)
are all monkeypatched, so these exercise the span planning / full-coverage /
cache / filler / concat logic in `author_split_bottoms` without spawning agents
or rendering anything.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import contenido_bionico.short.animate.orchestrator as o


def _write_scene(runs_root, seg, duration=5.0, t0=None, t1=None):
    d = runs_root / "9_short" / "animations" / str(seg)
    d.mkdir(parents=True, exist_ok=True)
    req = {
        "segment_id": seg,
        "timing": {"duration": duration,
                   "words": [{"word": "hola", "start": 0.1, "end": 0.4}]},
    }
    if t0 is not None:
        req["time_start"] = t0
        req["time_end"] = t1
    d.joinpath("Scene_Context.json").write_text(
        json.dumps({"scene_request": req}), encoding="utf-8",
    )


def _write_transcript(runs_root, words):
    (runs_root / "9_short").mkdir(parents=True, exist_ok=True)
    (runs_root / "9_short" / "transcript.json").write_text(
        json.dumps({"words": [
            {"text": w, "start": s, "end": e, "type": "word"}
            for w, s, e in words
        ]}),
        encoding="utf-8",
    )


def _patch_common(monkeypatch, runs_root, *, fail_keys=()):
    monkeypatch.setattr(o, "RUNS", runs_root)
    renders = []

    def fake_call_agent(agent, msg, vid, seg=None):
        assert agent == "short_author_split"
        if str(seg) in {str(k) for k in fail_keys}:
            raise o.ShortOrchestratorError("boom")
        p = o._split_seg_dir(vid, seg) / "SplitScene.tsx"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("import x;\nexport default SplitScene;", encoding="utf-8")
        return ""

    def fake_render(**kw):
        renders.append(kw)
        kw["out_mp4"].parent.mkdir(parents=True, exist_ok=True)
        kw["out_mp4"].write_bytes(b"mp4")
        return kw["out_mp4"]

    ffmpeg_calls = []

    def fake_ffmpeg(args, **kw):
        ffmpeg_calls.append(list(args))
        Path(args[-1]).write_bytes(b"out")                # emulate the output file
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(o, "call_agent", fake_call_agent)
    monkeypatch.setattr(o, "_copy_component_tsx", lambda **kw: runs_root / "staged.tsx")
    monkeypatch.setattr(o, "render_format_video", fake_render)
    monkeypatch.setattr(o.subprocess, "run", fake_ffmpeg)
    return SimpleNamespace(renders=renders, ffmpeg=ffmpeg_calls)


def _panel(runs_root, key):
    return runs_root / "9_short" / "split_animations" / str(key) / "bottom.mp4"


def _concat_list(runs_root):
    p = runs_root / "9_short" / "_intermediates" / "splitscreen_bottom_authored.txt"
    return [ln.split("'")[1] for ln in p.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- legacy mode -

def test_legacy_authors_all_and_concats(tmp_path, monkeypatch):
    # Contexts without time windows (pre-coverage runs): scene-only concat.
    _write_scene(tmp_path, 1)
    _write_scene(tmp_path, 2)
    _patch_common(monkeypatch, tmp_path)
    out = o.author_split_bottoms("9_short")
    assert out is not None and out.exists()
    assert out.name == "splitscreen_bottom_authored.mp4"
    assert _panel(tmp_path, 1).exists()
    assert _panel(tmp_path, 2).exists()


def test_legacy_skips_failed_scene(tmp_path, monkeypatch):
    _write_scene(tmp_path, 1)
    _write_scene(tmp_path, 2)
    _patch_common(monkeypatch, tmp_path, fail_keys=(1,))
    out = o.author_split_bottoms("9_short")
    assert out is not None and out.exists()              # seg 2 still ships
    assert not _panel(tmp_path, 1).exists()
    assert _panel(tmp_path, 2).exists()


def test_none_without_scenes(tmp_path, monkeypatch):
    _patch_common(monkeypatch, tmp_path)                 # no animations dir
    assert o.author_split_bottoms("9_short") is None


def test_reuses_cached_panel(tmp_path, monkeypatch):
    _write_scene(tmp_path, 1, duration=5.0, t0=2.0, t1=7.0)
    _write_transcript(tmp_path, [("uno", 0.5, 1.0), ("dos", 8.0, 8.4)])
    _patch_common(monkeypatch, tmp_path)
    calls = {"n": 0}
    inner = o.call_agent

    def counting(agent, msg, vid, seg=None):
        calls["n"] += 1
        return inner(agent, msg, vid, seg)

    monkeypatch.setattr(o, "call_agent", counting)
    o.author_split_bottoms("9_short", 12.0)              # first: authors every span
    first = calls["n"]
    o.author_split_bottoms("9_short", 12.0)              # second: reuses every panel
    assert first > 0
    assert calls["n"] == first


# ---------------------------------------------------------- full-coverage mode -

def test_full_coverage_spans_scene_windows_and_gaps(tmp_path, monkeypatch):
    # Scenes at [10,15] and [30,40] in a 50s video -> g1 | 1 | g2 | 2 | g3.
    _write_scene(tmp_path, 1, duration=5.0, t0=10.0, t1=15.0)
    _write_scene(tmp_path, 2, duration=10.0, t0=30.0, t1=40.0)
    _write_transcript(tmp_path, [("uno", 1.0, 1.5), ("dos", 20.0, 20.5),
                                 ("tres", 45.0, 45.5)])
    env = _patch_common(monkeypatch, tmp_path)
    out = o.author_split_bottoms("9_short", 50.0)
    assert out is not None
    names = [Path(c).parent.name for c in _concat_list(tmp_path)]
    assert names == ["g1", "1", "g2", "2", "g3"]
    # Coverage: span durations sum to total + the tail pad.
    durs = [r["duration_frames"] for r in env.renders]
    total = sum(durs) / o.DEFAULT_FPS
    assert abs(total - (50.0 + o.SPLIT_TAIL_PAD_SECONDS)) < 0.1
    # Gap context words are relative to the gap start.
    g2 = json.loads((tmp_path / "9_short" / "split_animations" / "g2" /
                     "Scene_Context.json").read_text(encoding="utf-8"))
    words = g2["scene_request"]["timing"]["words"]
    assert words == [{"word": "dos", "start": 5.0, "end": 5.5}]
    # Authored-only band -> stream-copy concat.
    concat = env.ffmpeg[-1]
    assert "copy" in concat


def test_full_coverage_absorbs_tiny_gap_into_next_scene(tmp_path, monkeypatch):
    # 1.2s gap between scenes (< SPLIT_MIN_GAP_SECONDS): no g-span between; the
    # second scene widens to start at 10.0 and its words shift by the gap.
    _write_scene(tmp_path, 1, duration=10.0, t0=0.0, t1=10.0)
    _write_scene(tmp_path, 2, duration=8.8, t0=11.2, t1=20.0)
    _write_transcript(tmp_path, [("uno", 0.5, 1.0)])
    env = _patch_common(monkeypatch, tmp_path)
    out = o.author_split_bottoms("9_short", 20.0)
    assert out is not None
    names = [Path(c).parent.name for c in _concat_list(tmp_path)]
    assert names == ["1", "2"]
    scene2 = next(r for r in env.renders
                  if r["props"]["segmentId"] == "split-2")
    dur2 = scene2["duration_frames"] / o.DEFAULT_FPS
    assert abs(dur2 - (10.0 + o.SPLIT_TAIL_PAD_SECONDS)) < 0.1   # widened + tail pad
    w = scene2["props"]["words"][0]
    assert abs(w["start"] - (0.1 + 1.2)) < 1e-6                  # shifted by the gap


def test_full_coverage_splits_long_gap_into_chunks(tmp_path, monkeypatch):
    # One scene at the very start of a 60s video: the 55s tail gap chunks into
    # ceil(55/25) = 3 spans.
    _write_scene(tmp_path, 1, duration=5.0, t0=0.0, t1=5.0)
    _write_transcript(tmp_path, [("uno", 10.0, 10.5)])
    _patch_common(monkeypatch, tmp_path)
    out = o.author_split_bottoms("9_short", 60.0)
    assert out is not None
    names = [Path(c).parent.name for c in _concat_list(tmp_path)]
    assert names == ["1", "g1", "g2", "g3"]


def test_full_coverage_failed_span_gets_filler_and_reencode(tmp_path, monkeypatch):
    _write_scene(tmp_path, 1, duration=5.0, t0=10.0, t1=15.0)
    _write_transcript(tmp_path, [("uno", 1.0, 1.5)])
    env = _patch_common(monkeypatch, tmp_path, fail_keys=("g1",))
    out = o.author_split_bottoms("9_short", 20.0)
    assert out is not None
    clips = _concat_list(tmp_path)
    assert clips[0].endswith("g1/filler.mp4")            # hole covered, order kept
    assert clips[1].endswith("1/bottom.mp4")
    concat = env.ffmpeg[-1]
    assert "copy" not in concat                          # filler -> re-encode
    assert "libx264" in concat


def test_full_coverage_rerenders_alone_when_duration_changes(tmp_path, monkeypatch):
    # Same tsx, new span duration (sidecar mismatch): the agent is NOT called
    # again; only the render re-runs.
    _write_scene(tmp_path, 1, duration=5.0, t0=0.0, t1=5.0)
    _write_transcript(tmp_path, [("uno", 6.0, 6.5)])
    env = _patch_common(monkeypatch, tmp_path)
    calls = {"n": 0}
    inner = o.call_agent

    def counting(agent, msg, vid, seg=None):
        calls["n"] += 1
        return inner(agent, msg, vid, seg)

    monkeypatch.setattr(o, "call_agent", counting)
    o.author_split_bottoms("9_short", 20.0)
    first_calls, first_renders = calls["n"], len(env.renders)
    o.author_split_bottoms("9_short", 30.0)              # tail gap grows: durations change
    assert calls["n"] == first_calls                     # no re-authoring
    assert len(env.renders) > first_renders              # but re-rendering happened
