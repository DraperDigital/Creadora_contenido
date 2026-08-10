"""Tests for Task 5 — the `rerender_part(video_id, part)` primitive.

`rerender_part` re-renders ONE part's asset from its CURRENT per-run
intermediate inputs. It never recomposes or publishes (that's `recompose`,
Task 6). No real Remotion/ffmpeg: `render_isolated_composition` (imported
into `short.animate.orchestrator` and called via its internals) is
monkeypatched to a stub that records how it was invoked and writes a 1-byte
placeholder webm, so these tests exercise only the pure Python dispatch/
wiring seam.

Dispatch tags come straight from the registry (`PARTS[...].rerender`):
captions, scene, audio are the only non-None tags; camera/carousel/
quotes must raise since they have no rerender entry.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import contenido_bionico.short.animate.orchestrator as orch
from contenido_bionico.short.change import primitives
from contenido_bionico.short.change.primitives import PrimitiveError, rerender_part
from contenido_bionico.short.change.registry import PARTS


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A fake run dir under a sandboxed RUNS root. `rerender_part` resolves
    the run dir via `orch._rd`, so pointing `orch.RUNS` at a tmp dir is
    sufficient to sandbox both `rerender_part` and everything it calls into
    inside `orch` — they always agree on the same directory."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(orch, "RUNS", runs_root)
    video_id = "999001_short"
    rd = runs_root / video_id
    rd.mkdir()
    return video_id, rd


@pytest.fixture
def render_stub(monkeypatch):
    """Stub `render_isolated_composition` (as imported into `orch`) so no
    real Remotion runs. Records every call's kwargs (minus the
    `write_compositions` callback, which is invoked immediately and its
    generated .ts captured instead, since that's what proves the RIGHT
    props/run_dir reached codegen) and writes a 1-byte placeholder webm."""
    calls: list[dict] = []

    def _stub(*, write_compositions, composition_id, out_webm, **kwargs):
        # Invoke the callback against a throwaway path so we capture the
        # generated Compositions TS the same way the real renderer would
        # have (it writes-then-renders), without touching REMOTION_SRC.
        generated_path = out_webm.with_suffix(".generated.ts")
        write_compositions(generated_path)
        generated_ts = generated_path.read_text("utf-8") if generated_path.exists() else ""
        calls.append(
            {
                "composition_id": composition_id,
                "out_webm": out_webm,
                "generated_ts": generated_ts,
                **kwargs,
            }
        )
        out_webm.parent.mkdir(parents=True, exist_ok=True)
        out_webm.write_bytes(b"\x00")
        return out_webm

    monkeypatch.setattr(orch, "render_isolated_composition", _stub)
    return calls


# ---------------------------------------------------------------------------
# Registry cross-check — dispatch table must never silently drift
# ---------------------------------------------------------------------------


def test_dispatch_tags_match_registry_rerender_tags():
    """The registry is the source of truth for which tags `rerender_part`
    must handle. Every non-None Part.rerender tag must be one this module
    dispatches on, and vice versa — no tag the registry doesn't know about."""
    registry_tags = {p.rerender for p in PARTS.values() if p.rerender}
    assert registry_tags == {"captions", "scene", "audio"}
    assert registry_tags == primitives.KNOWN_RERENDER_TAGS


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------


def test_captions_reads_persisted_props_json_directly(run_dir, render_stub, monkeypatch):
    """CAPTIONS re-render must read captions_props.json AS PERSISTED (which
    may carry proofread deltas `build_captions_props` cannot reproduce), not
    rebuild it. Guard this by making `build_captions_props` explode if
    called — the test fails loudly if rerender ever falls back to it."""
    video_id, rd = run_dir

    def _boom(_video_id):
        raise AssertionError("rerender_part must NOT call build_captions_props")

    monkeypatch.setattr(orch, "build_captions_props", _boom)

    props = {
        "durationSec": 7.5,
        "cues": [{"text": "PROOFREAD DELTA", "start": 0.0, "end": 1.0}],
        "placement": "center",
        "brand": {"font": "Inter"},
        "color": "#ffff00",
        "fontPx": 42,
    }
    (rd / orch.CAPTIONS_PROPS_FILE).write_text(
        json.dumps(props, ensure_ascii=False), encoding="utf-8"
    )

    result = rerender_part(video_id, "captions")

    assert result == rd / "captions.webm"
    assert result.exists()
    assert len(render_stub) == 1
    call = render_stub[0]
    assert call["composition_id"] == "captions"
    assert call["out_webm"] == rd / "captions.webm"
    assert call["require_alpha"] is True
    assert call["expected_duration"] == 7.5
    # The generated compositions TS embeds captions_props (incl. cues) as
    # defaultProps JSON -- assert the LOADED (proofread-delta-carrying)
    # props reached codegen verbatim, proving no re-derivation happened.
    assert "PROOFREAD DELTA" in call["generated_ts"]
    assert "#ffff00" in call["generated_ts"]
    # No per-run Captions.tsx staged -> shared import (no run_dir copy present).
    assert 'import { Captions } from "./Captions";' in call["generated_ts"]


def test_captions_uses_per_run_tsx_when_present(run_dir, render_stub, remotion_src_sandbox):
    """Passing run_dir=rd to write_compositions_ts must pick up a per-run
    remotion/Captions.tsx copy (Task 3) when present."""
    video_id, rd = run_dir
    (remotion_src_sandbox / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null;\nexport default Captions;\n",
        encoding="utf-8",
    )
    (rd / "remotion").mkdir()
    (rd / "remotion" / "Captions.tsx").write_text(
        'import type { CaptionsProps } from "./lib/types";\n'
        "export const Captions = () => null; // PATCHED\n"
        "export default Captions;\n",
        encoding="utf-8",
    )
    props = {
        "durationSec": 3.0,
        "cues": [],
        "placement": "center",
        "brand": {"font": "X"},
        "color": None,
        "fontPx": 40,
    }
    (rd / orch.CAPTIONS_PROPS_FILE).write_text(
        json.dumps(props, ensure_ascii=False), encoding="utf-8"
    )

    rerender_part(video_id, "captions")

    call = render_stub[0]
    assert f'from "./runs/{video_id}/Captions"' in call["generated_ts"]
    assert 'from "./Captions";' not in call["generated_ts"]
    staged = remotion_src_sandbox / "runs" / video_id / "Captions.tsx"
    assert staged.exists()
    assert "PATCHED" in staged.read_text("utf-8")


def test_captions_missing_props_json_raises(run_dir, render_stub):
    """An empty run dir has nothing to rebuild captions_props.json FROM
    either (no transcript.json/source.mp4), so `build_captions_props`
    returns None and rerender still raises -- distinct from the fallback
    case below where the run DOES have rebuildable data."""
    video_id, rd = run_dir
    with pytest.raises(PrimitiveError):
        rerender_part(video_id, "captions")
    assert render_stub == []


def test_captions_missing_props_json_rebuilds_via_build_captions_props(
    run_dir, render_stub, monkeypatch
):
    """A run produced before Task 2 (or forked from one) has no
    captions_props.json. rerender_part must reconstruct it by calling
    `orch.build_captions_props`, PERSIST the result to captions_props.json
    (so subsequent calls read it directly), then render from it -- instead
    of crashing."""
    video_id, rd = run_dir
    props_path = rd / orch.CAPTIONS_PROPS_FILE
    assert not props_path.exists()

    rebuilt = {
        "durationSec": 9.0,
        "cues": [{"text": "REBUILT", "start": 0.0, "end": 1.0}],
        "placement": "center",
        "brand": {"font": "Inter"},
        "color": None,
        "fontPx": 64,
    }
    calls: list = []

    def _fake_build(vid):
        calls.append(vid)
        return rebuilt

    monkeypatch.setattr(orch, "build_captions_props", _fake_build)

    result = rerender_part(video_id, "captions")

    assert calls == [video_id]
    # Persisted to disk so a later call (or a human/agent) reads it directly.
    assert props_path.exists()
    persisted = json.loads(props_path.read_text(encoding="utf-8"))
    assert persisted == rebuilt
    # And the render itself used the rebuilt props.
    assert result == rd / "captions.webm"
    assert result.exists()
    assert len(render_stub) == 1
    assert render_stub[0]["expected_duration"] == 9.0
    assert "REBUILT" in render_stub[0]["generated_ts"]


def test_captions_missing_props_json_raises_when_unrebuildable(
    run_dir, render_stub, monkeypatch
):
    """If the run genuinely has no data to rebuild captions from (e.g. no
    transcript/source), `build_captions_props` returns None and rerender
    must raise PrimitiveError with a clear message, not crash on a missing
    file read."""
    video_id, rd = run_dir

    monkeypatch.setattr(orch, "build_captions_props", lambda vid: None)

    with pytest.raises(PrimitiveError):
        rerender_part(video_id, "captions")
    assert render_stub == []
    assert not (rd / orch.CAPTIONS_PROPS_FILE).exists()


# ---------------------------------------------------------------------------
# scene
# ---------------------------------------------------------------------------


def _write_scene(rd: Path, segment_id: int, *, with_tsx: bool = True) -> Path:
    seg_dir = rd / "animations" / str(segment_id)
    seg_dir.mkdir(parents=True)
    if with_tsx:
        (seg_dir / "Scene.tsx").write_text(
            "export const Scene = () => null;\nexport default Scene;\n",
            encoding="utf-8",
        )
    scene_request = {
        "segment_id": segment_id,
        "timing": {
            "duration": 2.5,
            "words": [{"word": "hola", "start": 0.0, "end": 0.4}],
        },
    }
    context = {
        "video_id": segment_id,
        "segment_id": segment_id,
        "scene_request": scene_request,
    }
    (seg_dir / "Scene_Context.json").write_text(
        json.dumps(context, ensure_ascii=False), encoding="utf-8"
    )
    return seg_dir


def test_scene_rerenders_every_segment_with_a_scene_tsx(run_dir, render_stub):
    video_id, rd = run_dir
    _write_scene(rd, 1)
    _write_scene(rd, 2)

    result = rerender_part(video_id, "scene")

    # One render call per segment, each driven by its own Scene_Context.json
    # timing (duration/words) -- not re-derived from a plan file.
    assert len(render_stub) == 2
    comp_ids = {c["composition_id"] for c in render_stub}
    assert comp_ids == {"segment-1", "segment-2"}
    assert (rd / "animations" / "1" / "animation.webm").exists()
    assert (rd / "animations" / "2" / "animation.webm").exists()
    # Returns a path (the last rendered scene's webm) per the Path | None contract.
    assert result in {
        rd / "animations" / "1" / "animation.webm",
        rd / "animations" / "2" / "animation.webm",
    }


def test_scene_orders_segments_numerically_not_lexicographically(run_dir, render_stub):
    """Segment dir names are bare integers ("1", "2", ... "10"); a
    lexicographic sort would put "10" before "2". Render order (and thus
    which webm `rerender_part` returns as "last") must follow numeric
    segment_id order."""
    video_id, rd = run_dir
    _write_scene(rd, 2)
    _write_scene(rd, 10)

    result = rerender_part(video_id, "scene")

    rendered_order = [c["composition_id"] for c in render_stub]
    assert rendered_order == ["segment-2", "segment-10"]
    assert result == rd / "animations" / "10" / "animation.webm"


def test_scene_skips_segments_without_scene_tsx(run_dir, render_stub):
    video_id, rd = run_dir
    _write_scene(rd, 1)
    _write_scene(rd, 2, with_tsx=False)  # Scene_Context.json but no Scene.tsx

    rerender_part(video_id, "scene")

    assert len(render_stub) == 1
    assert render_stub[0]["composition_id"] == "segment-1"


def test_scene_uses_scene_context_timing_not_replan(run_dir, render_stub):
    """The rendered duration must come from the segment's own
    Scene_Context.json (a look-only re-render), never from re-invoking the
    planner."""
    video_id, rd = run_dir
    _write_scene(rd, 1)

    rerender_part(video_id, "scene")

    assert render_stub[0]["expected_duration"] == 2.5


def test_scene_no_segments_returns_none(run_dir, render_stub):
    video_id, rd = run_dir
    (rd / "animations").mkdir()
    result = rerender_part(video_id, "scene")
    assert result is None
    assert render_stub == []


def test_scene_no_animations_dir_returns_none(run_dir, render_stub):
    video_id, rd = run_dir
    result = rerender_part(video_id, "scene")
    assert result is None
    assert render_stub == []


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------


def test_audio_returns_none_and_never_renders(run_dir, render_stub):
    video_id, rd = run_dir
    (rd / "Audio_Plan.json").write_text(
        json.dumps({"music": None, "sfx_cues": []}), encoding="utf-8"
    )
    result = rerender_part(video_id, "audio")
    assert result is None
    assert render_stub == []


# ---------------------------------------------------------------------------
# manifest-only / respawn-only parts -> raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part", ["camera", "carousel", "quotes"])
def test_manifest_or_respawn_only_parts_raise(run_dir, render_stub, part):
    video_id, rd = run_dir
    with pytest.raises(PrimitiveError):
        rerender_part(video_id, part)
    assert render_stub == []


def test_unknown_part_raises(run_dir, render_stub):
    """An unrecognized part key is a bad-input error, distinct from a valid
    part with no rerender dispatch -- rerender_part must not swallow
    `part_for`'s own already-tested KeyError contract into PrimitiveError."""
    video_id, rd = run_dir
    with pytest.raises(KeyError):
        rerender_part(video_id, "not-a-real-part")
    assert render_stub == []


def test_cut_part_raises():
    """`cut` has no rerender tag (respawn="recut_and_rederive" only) -- must
    raise without even needing a run dir fixture, same as camera/carousel/
    quotes."""
    with pytest.raises(PrimitiveError):
        rerender_part("anything", "cut")


# ---------------------------------------------------------------------------
# rerender_part never recomposes or publishes
# ---------------------------------------------------------------------------


def test_captions_rerender_does_not_touch_final_mp4(run_dir, render_stub):
    """rerender_part re-renders ONE asset only; it must never assemble,
    mix, or publish. Guard by asserting final.mp4 is untouched."""
    video_id, rd = run_dir
    props = {
        "durationSec": 1.0, "cues": [], "placement": "center",
        "brand": {"font": "X"}, "color": None, "fontPx": 40,
    }
    (rd / orch.CAPTIONS_PROPS_FILE).write_text(json.dumps(props), encoding="utf-8")
    final_before = rd / "final.mp4"
    assert not final_before.exists()

    rerender_part(video_id, "captions")

    assert not final_before.exists()
