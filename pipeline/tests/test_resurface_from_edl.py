"""Unit tests for the deterministic `resurface_from_edl` re-render entry.

`resurface_from_edl` replays only the deterministic tail of the cut
(de-silence-if-absent -> render_edl -> derive_transcript -> verify_contract)
from an already-edited `_intermediates/edl_render.json`, skipping the whole
editor / reviewer / mapper recovery loop. These tests monkeypatch `run_step`
onto a recorder and `verify_contract` / `reap_sidecars` into no-ops, so NO real
ffmpeg / desilence / derive helper ever runs — the run dir holds empty
placeholder files only. RUNS is repointed at tmp_path so nothing touches the
real pipeline/runs tree.
"""
import contenido_bionico.shared.cut.orchestrator as orch
import pytest


def _touch(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _mk_run(runs_root, video_id, *, with_desilenced=False):
    """Create a fake run dir with the files resurface needs (all placeholders)."""
    run_dir = runs_root / str(video_id)
    intermediates = run_dir / "_intermediates"
    _touch(run_dir / "raw.mp4")
    _touch(intermediates / "edl_render.json", b"{}")
    _touch(intermediates / "word_for_word.json", b"{}")
    if with_desilenced:
        _touch(run_dir / "desilenced.mp4")
    return run_dir, intermediates


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """Repoint RUNS at tmp_path, record run_step calls, stub the tail's
    non-subprocess side effects (verify_contract / reap_sidecars)."""
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(orch, "RUNS", runs_root)
    calls = []
    monkeypatch.setattr(orch, "run_step", lambda label, cmd: calls.append((label, cmd)))
    monkeypatch.setattr(orch, "verify_contract", lambda run_dir: None)
    # Strong contract validator is a real subprocess on source.mp4/transcript.json;
    # stub it to "passed" here (its failure path has its own test below).
    monkeypatch.setattr(orch, "run_cut_contract_validator", lambda run_dir: (True, []))
    monkeypatch.setattr(orch, "reap_sidecars", lambda: None)
    return runs_root, calls


def _labels(calls):
    return [label for label, _ in calls]


def _cmd_for(calls, needle):
    """Return the recorded cmd list whose joined string contains `needle`."""
    for _, cmd in calls:
        if any(needle in part for part in cmd):
            return cmd
    raise AssertionError(f"no recorded command referencing {needle!r}: {calls}")


def test_desilence_recorded_before_render_when_desilenced_absent(recorder):
    runs_root, calls = recorder
    _mk_run(runs_root, 7, with_desilenced=False)

    orch.resurface_from_edl(7)

    labels = _labels(calls)
    # (a) a desilence step is recorded, and it comes BEFORE the render step.
    assert "de-silence video" in labels
    assert "render source.mp4" in labels
    assert labels.index("de-silence video") < labels.index("render source.mp4")
    # The de-silence step uses the SAME --cap-ms 120 param and reads raw.mp4.
    desil = _cmd_for(calls, "desilence.py")
    assert "--cap-ms" in desil
    assert desil[desil.index("--cap-ms") + 1] == "120"
    assert any(part.endswith("raw.mp4") for part in desil)


def test_desilence_skipped_when_desilenced_present(recorder):
    runs_root, calls = recorder
    _mk_run(runs_root, 8, with_desilenced=True)

    orch.resurface_from_edl(8)

    assert "de-silence video" not in _labels(calls)


def test_render_edl_uses_desilenced_and_edl_render_to_source(recorder):
    runs_root, calls = recorder
    _mk_run(runs_root, 9, with_desilenced=False)

    orch.resurface_from_edl(9)

    # (b) render_edl gets the de-silenced file + edl_render.json -> source.mp4.
    render = _cmd_for(calls, "render_edl.py")
    joined = " ".join(render)
    assert "edl_render.json" in joined
    assert any(part.endswith("desilenced.mp4") for part in render)
    assert "-o" in render
    assert render[render.index("-o") + 1].endswith("source.mp4")


def test_render_edl_uses_raw_when_keep_silences(recorder):
    runs_root, calls = recorder
    _mk_run(runs_root, 10, with_desilenced=False)

    orch.resurface_from_edl(10, keep_silences=True)

    # keep_silences renders straight from raw.mp4, no de-silence step.
    assert "de-silence video" not in _labels(calls)
    render = _cmd_for(calls, "render_edl.py")
    assert any(part.endswith("raw.mp4") for part in render)


def test_derive_transcript_uses_word_for_word_and_edl_render(recorder):
    runs_root, calls = recorder
    _mk_run(runs_root, 11, with_desilenced=True)

    orch.resurface_from_edl(11)

    # (c) derive_transcript gets word_for_word.json + edl_render.json -> transcript.json.
    derive = _cmd_for(calls, "derive_transcript.py")
    joined = " ".join(derive)
    assert "word_for_word.json" in joined
    assert "edl_render.json" in joined
    assert "-o" in derive
    assert derive[derive.index("-o") + 1].endswith("transcript.json")


def test_missing_edl_render_raises(recorder):
    runs_root, calls = recorder
    run_dir, intermediates = _mk_run(runs_root, 12, with_desilenced=True)
    # (d) drop edl_render.json -> guard must raise the module's error type.
    (intermediates / "edl_render.json").unlink()

    with pytest.raises(orch.PrepOrchestratorError):
        orch.resurface_from_edl(12)
    # Guard fires before any step runs.
    assert calls == []


def test_missing_raw_mp4_raises(recorder):
    runs_root, calls = recorder
    run_dir, intermediates = _mk_run(runs_root, 13, with_desilenced=True)
    (run_dir / "raw.mp4").unlink()

    with pytest.raises(orch.PrepOrchestratorError):
        orch.resurface_from_edl(13)
    assert calls == []


def test_missing_word_for_word_raises(recorder):
    runs_root, calls = recorder
    run_dir, intermediates = _mk_run(runs_root, 14, with_desilenced=True)
    (intermediates / "word_for_word.json").unlink()

    with pytest.raises(orch.PrepOrchestratorError):
        orch.resurface_from_edl(14)
    assert calls == []


def test_contract_validator_failure_raises(recorder, monkeypatch):
    # A resurface that renders but fails the strong contract validator (source
    # audio vs transcript misaligned -- e.g. a de-silence timeline drift) must
    # raise, matching the full cut's guarantee, not ship silently.
    runs_root, calls = recorder
    _mk_run(runs_root, 15, with_desilenced=True)
    monkeypatch.setattr(orch, "run_cut_contract_validator",
                        lambda run_dir: (False, ["source/transcript misaligned"]))

    with pytest.raises(orch.PrepOrchestratorError):
        orch.resurface_from_edl(15)
    # It rendered first (steps ran), then failed the validator.
    assert "render source.mp4" in _labels(calls)
