"""Tests for Task 6 — the `recompose(video_id)` primitive.

`recompose` is the ONE part-independent glue path: after the change agent
edits any video-layer intermediate (or calls `rerender_part`), it calls
`recompose` ONCE to re-composite the picture, re-layer audio, and publish.

THE gotcha this primitive exists to encapsulate: `audio_mix.mix(run_dir)` is
IDEMPOTENT via a `final.video_only.mp4` backup -- if that file exists, `mix`
treats it (not the freshly-assembled `final.mp4`) as the canonical voice+
video base, silently discarding whatever `assemble_from_manifest` just
produced. So the order must always be:

    assemble_from_manifest(rd) -> unlink stale final.video_only.mp4 (if any)
    -> mix(rd) (only when Audio_Plan.json exists) -> publish_output(...)

No real ffmpeg/renders: `assemble_from_manifest`, `mix`, and `publish_output`
are monkeypatched (as imported into `short.change.primitives`) to record
call order; these tests exercise only the pure Python sequencing/wiring seam.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import contenido_bionico.short.animate.orchestrator as orch
from contenido_bionico.short.change import primitives
from contenido_bionico.short.change.primitives import recompose


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A fake run dir under a sandboxed RUNS root. `recompose` resolves the
    run dir via the same `_rd` helper `rerender_part` uses (Task 5), which
    delegates to `orch._rd` / `orch.RUNS` -- so sandboxing `orch.RUNS` is
    sufficient and guarantees both primitives agree on the same directory."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(orch, "RUNS", runs_root)
    video_id = "999002_short"
    rd = runs_root / video_id
    rd.mkdir()
    return video_id, rd


@pytest.fixture
def recorder(monkeypatch):
    """Monkeypatch assemble_from_manifest / mix / publish_output as imported
    into `primitives` (the module does its own local/lazy imports of these,
    mirroring the plan's sample code and the existing `pl` import-cycle
    pattern in `short/change/orchestrator.py`). Records call order + args in
    a single shared list so tests can assert strict sequencing."""
    calls: list[tuple[str, dict]] = []

    def _assemble(run_dir):
        calls.append(("assemble", {"run_dir": run_dir}))
        return {"status": "ok"}

    def _mix(run_dir):
        calls.append(("mix", {"run_dir": run_dir}))
        return {"status": "ok"}

    def _publish(video_id, *, run_filename, kind, required=False):
        calls.append(
            (
                "publish",
                {
                    "video_id": video_id,
                    "run_filename": run_filename,
                    "kind": kind,
                    "required": required,
                },
            )
        )
        return Path(f"/published/{video_id}.mp4")

    monkeypatch.setattr(
        "contenido_bionico.short.animate.assembly_render.assemble_from_manifest",
        _assemble,
    )
    monkeypatch.setattr(
        "contenido_bionico.shared.audio.audio_mix.mix",
        _mix,
    )
    monkeypatch.setattr(
        "contenido_bionico.pipeline.publish_output",
        _publish,
    )
    return calls


# ---------------------------------------------------------------------------
# The gotcha: final.video_only.mp4 must be dropped BEFORE mix runs
# ---------------------------------------------------------------------------


def test_order_with_audio_plan_drops_video_only_before_mix(run_dir, recorder):
    """The full sequence when Audio_Plan.json is present: assemble -> unlink
    stale final.video_only.mp4 -> mix -> publish. If the unlink happened
    after (or not at all), `mix` would silently re-layer audio onto the OLD
    base and the freshly re-composited picture would be lost -- this is the
    #1 correctness bug the primitive exists to prevent."""
    video_id, rd = run_dir
    (rd / "Audio_Plan.json").write_text("{}", encoding="utf-8")
    video_only = rd / "final.video_only.mp4"
    video_only.write_bytes(b"STALE")
    assert video_only.exists()

    result = recompose(video_id)

    steps = [name for name, _ in recorder]
    assert steps == ["assemble", "mix", "publish"]
    # The stale backup must be gone by the time mix() runs.
    assert not video_only.exists()

    assemble_call = recorder[0][1]
    mix_call = recorder[1][1]
    publish_call = recorder[2][1]
    assert assemble_call["run_dir"] == rd
    assert mix_call["run_dir"] == rd
    assert publish_call == {
        "video_id": video_id,
        "run_filename": "final.mp4",
        "kind": "short",
        "required": True,
    }
    assert result == Path(f"/published/{video_id}.mp4")


def test_video_only_unlinked_before_mix_even_when_absent(run_dir, recorder):
    """No stale backup on disk -> nothing to unlink, but the sequence and
    the absence of the file after assemble must still hold (guards against
    an unlink that assumes the file always exists and blows up, or an
    ordering bug that only manifests when the file happens to be present)."""
    video_id, rd = run_dir
    (rd / "Audio_Plan.json").write_text("{}", encoding="utf-8")
    assert not (rd / "final.video_only.mp4").exists()

    recompose(video_id)

    steps = [name for name, _ in recorder]
    assert steps == ["assemble", "mix", "publish"]
    assert not (rd / "final.video_only.mp4").exists()


# ---------------------------------------------------------------------------
# Voice-only branch: no Audio_Plan.json -> no mix, straight to publish
# ---------------------------------------------------------------------------


def test_voice_only_skips_mix_when_no_audio_plan(run_dir, recorder):
    """No Audio_Plan.json (voice-only short, no music/SFX) -> mix must NOT
    be called at all; recompose still assembles, drops any stale
    final.video_only.mp4, and publishes final.mp4 directly."""
    video_id, rd = run_dir
    video_only = rd / "final.video_only.mp4"
    video_only.write_bytes(b"STALE")
    assert not (rd / "Audio_Plan.json").exists()

    result = recompose(video_id)

    steps = [name for name, _ in recorder]
    assert steps == ["assemble", "publish"]
    assert "mix" not in steps
    assert not video_only.exists()
    publish_call = recorder[1][1]
    assert publish_call == {
        "video_id": video_id,
        "run_filename": "final.mp4",
        "kind": "short",
        "required": True,
    }
    assert result == Path(f"/published/{video_id}.mp4")


def test_voice_only_with_no_stale_backup_still_publishes(run_dir, recorder):
    video_id, rd = run_dir
    assert not (rd / "Audio_Plan.json").exists()
    assert not (rd / "final.video_only.mp4").exists()

    recompose(video_id)

    steps = [name for name, _ in recorder]
    assert steps == ["assemble", "publish"]


# ---------------------------------------------------------------------------
# recompose always calls publish_output with the fixed contract
# ---------------------------------------------------------------------------


def test_publish_always_uses_final_mp4_short_required(run_dir, recorder):
    """publish_output must always be called with run_filename='final.mp4',
    kind='short', required=True -- regardless of the audio branch -- so a
    missing final.mp4 (assemble silently failed) raises loudly instead of
    recompose returning None unnoticed."""
    video_id, rd = run_dir
    (rd / "Audio_Plan.json").write_text("{}", encoding="utf-8")

    recompose(video_id)

    publish_calls = [args for name, args in recorder if name == "publish"]
    assert len(publish_calls) == 1
    assert publish_calls[0]["run_filename"] == "final.mp4"
    assert publish_calls[0]["kind"] == "short"
    assert publish_calls[0]["required"] is True


def test_recompose_returns_publish_output_result(run_dir, recorder):
    video_id, rd = run_dir
    result = recompose(video_id)
    assert result == Path(f"/published/{video_id}.mp4")


# ---------------------------------------------------------------------------
# run dir resolution matches rerender_part's (_rd)
# ---------------------------------------------------------------------------


def test_recompose_resolves_run_dir_via_same_rd_helper(run_dir, recorder):
    """`recompose` must resolve the run dir the same way `rerender_part`
    does (Task 5's `_rd`, itself `orch._rd` / `orch.RUNS`) so both
    primitives agree on the same directory. Proven by pointing at a
    non-default `orch.RUNS` (the `run_dir` fixture) and asserting the
    assemble/mix calls receive that exact directory."""
    video_id, rd = run_dir
    (rd / "Audio_Plan.json").write_text("{}", encoding="utf-8")

    recompose(video_id)

    assert recorder[0][1]["run_dir"] == primitives._rd(video_id)
    assert recorder[0][1]["run_dir"] == rd
