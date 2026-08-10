"""Tests for Task 9 — CUT re-entry: notes injection + `recut` + `recut_and_rederive`.

Three pure-Python seams, none of which launches a real editor/reviewer agent, a
real cut render, or a real animate:

9a. `shared.cut.runner.build_editor_message` / `build_reviewer_message` append a
    DELETION-ONLY `<USER_CHANGE_REQUEST>` block iff `_intermediates/recut_notes.txt`
    exists (and omit it otherwise). Exercised against a seeded `verbose.txt` +
    optional notes file; the message is a pure string, no agent runs.

9b. `shared.cut.orchestrator.recut` writes `recut_notes.txt` BEFORE
    `reset_cut_attempt`, drives the shared cut body (the editor-reviewer-loop
    `run_step` is invoked, i.e. the final.txt clearing happened), and raises when
    `raw.mp4` is absent. `run_step` / `reset_cut_attempt` / `_run_cut_body` and the
    heavy tail helpers are monkeypatched so nothing subprocesses.

9c. `pipeline.recut_and_rederive` order is recut -> full rebuild
    (`run_short_phase(force=True, build_extras=False)`) with `anim_opts` flowing
    through. `recut` and `run_short_phase` are monkeypatched to record calls.
"""
from __future__ import annotations

import pytest

import contenido_bionico.shared.cut.runner as runner
import contenido_bionico.shared.cut.orchestrator as cut_orch
import contenido_bionico.pipeline as pl


# ===========================================================================
# 9a. runner: DELETION-ONLY <USER_CHANGE_REQUEST> block in editor + reviewer
# ===========================================================================


@pytest.fixture
def cut_run(tmp_path, monkeypatch):
    """A fake cut run dir under a sandboxed runner.RUNS. `build_editor_message`
    / `build_reviewer_message` resolve inputs via runner.RUNS + run_number, so
    pointing runner.RUNS at tmp and using a parseable id sandboxes them."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(runner, "RUNS", runs_root)
    video_id = "1_short"
    inter = runs_root / video_id / "_intermediates"
    (inter / "editor").mkdir(parents=True)
    (inter / "reviewer").mkdir(parents=True)
    (inter / "verbose.txt").write_text(
        "hola esto es la transcripcion completa en prosa del video",
        encoding="utf-8",
    )
    return video_id, runs_root / video_id, inter


def test_editor_message_omits_change_request_block_without_notes(cut_run):
    video_id, _rd, _inter = cut_run
    msg = runner.build_editor_message(video_id, "01")
    assert "<USER_CHANGE_REQUEST>" not in msg


def test_editor_message_includes_deletion_only_block_with_notes(cut_run):
    video_id, _rd, inter = cut_run
    (inter / "recut_notes.txt").write_text(
        "quita la intro larga y ve directo al punto", encoding="utf-8"
    )
    msg = runner.build_editor_message(video_id, "01")
    assert "<USER_CHANGE_REQUEST>" in msg
    # The user's request text is embedded.
    assert "quita la intro larga" in msg
    # The appended instruction forbids adding words (deletion-only contract).
    lower = msg.lower()
    assert "deletion-only" in lower
    assert "never add" in lower


def test_reviewer_message_includes_deletion_only_block_with_notes(cut_run):
    video_id, _rd, inter = cut_run
    # The reviewer message also reads the editor's proposal for this iteration.
    (inter / "editor" / "proposal_1.txt").write_text(
        "hola esto es la transcripcion", encoding="utf-8"
    )
    (inter / "recut_notes.txt").write_text(
        "acorta el final", encoding="utf-8"
    )
    msg = runner.build_reviewer_message(video_id, "01")
    assert "<USER_CHANGE_REQUEST>" in msg
    assert "acorta el final" in msg
    lower = msg.lower()
    # Reviewer is told to REJECT proposals that add/insert/rewrite words.
    assert "deletion-only" in lower
    assert "reject" in lower


def test_reviewer_message_omits_change_request_block_without_notes(cut_run):
    video_id, _rd, inter = cut_run
    (inter / "editor" / "proposal_1.txt").write_text(
        "hola esto es la transcripcion", encoding="utf-8"
    )
    msg = runner.build_reviewer_message(video_id, "01")
    assert "<USER_CHANGE_REQUEST>" not in msg


def test_read_recut_notes_absent_returns_none(cut_run):
    video_id, _rd, _inter = cut_run
    assert runner.read_recut_notes(video_id) is None


def test_read_recut_notes_present_returns_text(cut_run):
    video_id, _rd, inter = cut_run
    (inter / "recut_notes.txt").write_text("mis notas", encoding="utf-8")
    assert runner.read_recut_notes(video_id) == "mis notas"


# ===========================================================================
# 9b. orchestrator.recut: notes-before-reset, loop invoked, raw.mp4 guard
# ===========================================================================


@pytest.fixture
def recut_run(tmp_path, monkeypatch):
    """A fake run dir under a sandboxed cut_orch.RUNS. `recut` resolves its run
    dir via `prepare_folders` -> cut_orch.RUNS."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(cut_orch, "RUNS", runs_root)
    video_id = "1_short"
    rd = runs_root / video_id
    (rd / "_intermediates").mkdir(parents=True)
    return video_id, rd


def test_recut_raises_when_raw_mp4_missing(recut_run, monkeypatch):
    video_id, rd = recut_run
    # No raw.mp4 written -> guard must fire before any body step.
    called = {"body": False}

    def _body(*a, **k):
        called["body"] = True

    monkeypatch.setattr(cut_orch, "_run_cut_body", _body)
    with pytest.raises(cut_orch.PrepOrchestratorError) as exc:
        cut_orch.recut(video_id, "algo")
    assert "raw.mp4" in str(exc.value)
    assert called["body"] is False


def test_recut_writes_notes_before_reset_then_runs_body(recut_run, monkeypatch):
    video_id, rd = recut_run
    (rd / "raw.mp4").write_bytes(b"\x00rawvideo")
    notes_path = rd / "_intermediates" / "recut_notes.txt"
    order: list[str] = []

    def _reset(intermediates, run_dir):
        # The notes file MUST already be on disk when reset runs, so the
        # re-run loop sees it from iteration 1.
        order.append("reset")
        assert notes_path.exists(), "recut_notes.txt must be written BEFORE reset_cut_attempt"

    def _body(vid, raw_mp4, intermediates, run_dir, *, keep_silences=False, comment_mode=False):
        order.append("body")
        assert raw_mp4 == rd / "raw.mp4"
        # recut always drives the standard (non keep-silences, non comment) path.
        assert keep_silences is False
        assert comment_mode is False

    monkeypatch.setattr(cut_orch, "reset_cut_attempt", _reset)
    monkeypatch.setattr(cut_orch, "_run_cut_body", _body)

    cut_orch.recut(video_id, "quita la intro")

    assert order == ["reset", "body"]
    assert notes_path.read_text(encoding="utf-8").strip() == "quita la intro"


def test_recut_none_notes_writes_empty_notes_file(recut_run, monkeypatch):
    video_id, rd = recut_run
    (rd / "raw.mp4").write_bytes(b"\x00rawvideo")
    monkeypatch.setattr(cut_orch, "reset_cut_attempt", lambda *a, **k: None)
    monkeypatch.setattr(cut_orch, "_run_cut_body", lambda *a, **k: None)

    cut_orch.recut(video_id, None)

    # notes=None still writes the file (its presence is what triggers the loop's
    # notes-aware branch); a None simply means no extra steering text.
    assert (rd / "_intermediates" / "recut_notes.txt").exists()


def test_recut_body_invokes_editor_reviewer_loop_step(recut_run, monkeypatch):
    """With the tail helpers stubbed, `_run_cut_body` runs as pure sequencing:
    assert the 'editor reviewer loop' run_step is invoked (proving final.txt
    clearing + loop re-run path was entered), not skipped."""
    video_id, rd = recut_run
    (rd / "raw.mp4").write_bytes(b"\x00rawvideo")
    inter = rd / "_intermediates"
    # Seed the immutable base transcript so write_verbose_and_word_for_word (stubbed
    # anyway) and the reuse guards behave; a de-silenced video + raw transcript
    # present so the body skips the de-silence + transcribe run_steps.
    (rd / "desilenced.mp4").write_bytes(b"\x00")
    (inter / "raw_transcript.json").write_text('{"text": "hola", "words": []}', encoding="utf-8")
    (inter / "word_for_word.json").write_text('{"text": "hola", "words": []}', encoding="utf-8")

    steps: list[str] = []

    def _run_step(label, cmd):
        steps.append(label)

    monkeypatch.setattr(cut_orch, "run_step", _run_step)
    # Neutralize the heavy tail so the body is pure sequencing.
    monkeypatch.setattr(cut_orch, "write_verbose_and_word_for_word", lambda inter: None)
    monkeypatch.setattr(cut_orch, "run_mapper_with_finalizer_recovery", lambda vid, inter: None)
    monkeypatch.setattr(cut_orch, "verify_contract", lambda run_dir: None)
    monkeypatch.setattr(cut_orch, "run_cut_contract_validator", lambda run_dir: (True, []))
    monkeypatch.setattr(cut_orch, "reap_sidecars", lambda: None)
    monkeypatch.setattr(cut_orch, "reset_cut_attempt", lambda *a, **k: None)

    cut_orch.recut(video_id, "quita la intro")

    assert "editor reviewer loop" in steps
    # Reuse guards: with desilenced.mp4 + raw_transcript.json present, the body
    # must NOT re-run de-silence or transcription.
    assert "de-silence video" not in steps
    assert "transcribe raw MP4" not in steps


# ===========================================================================
# 9c. pipeline.recut_and_rederive: recut -> full rebuild, anim_opts threaded
# ===========================================================================


@pytest.fixture
def rederive_calls(monkeypatch):
    """Monkeypatch `recut` (module global in pipeline) and `run_short_phase` so
    `recut_and_rederive` exercises only the pure orchestration seam. Records the
    ordered calls. The IRON LAW gate (BIONICO_ALLOW_FULL_REBUILD, default
    blocked) is opened so the orchestration itself can be exercised."""
    monkeypatch.setenv("BIONICO_ALLOW_FULL_REBUILD", "1")
    calls: list[tuple[str, dict]] = []

    def _recut(video_id, notes):
        calls.append(("recut", {"video_id": video_id, "notes": notes}))

    def _run_short_phase(video_id, **kwargs):
        calls.append(("run_short_phase", {"video_id": video_id, **kwargs}))
        return object()  # a sentinel "published path"

    monkeypatch.setattr(pl, "recut", _recut)
    monkeypatch.setattr(pl, "run_short_phase", _run_short_phase)
    return calls


def test_recut_and_rederive_order_is_recut_then_full_rebuild(rederive_calls):
    pl.recut_and_rederive("8_short", "quita la intro larga")
    names = [c[0] for c in rederive_calls]
    assert names == ["recut", "run_short_phase"]
    # recut got the notes verbatim.
    assert rederive_calls[0][1] == {"video_id": "8_short", "notes": "quita la intro larga"}


def test_recut_and_rederive_rebuild_forces_and_skips_extras(rederive_calls):
    pl.recut_and_rederive("8_short", "acorta")
    _name, kwargs = rederive_calls[1]
    # Full rebuild: force=True (re-plan scenes at new timing), build_extras=False
    # (forked carousel/quotes are reused, not regenerated).
    assert kwargs["force"] is True
    assert kwargs["build_extras"] is False


def test_recut_and_rederive_threads_anim_opts(rederive_calls):
    anim_opts = {
        "anim_quality": "alta",
        "no_music": True,
        "anim_count": "few",
    }
    pl.recut_and_rederive("8_short", "acorta", anim_opts=anim_opts)
    _name, kwargs = rederive_calls[1]
    # Each anim_opt flows through to the rebuild.
    assert kwargs["anim_quality"] == "alta"
    assert kwargs["no_music"] is True
    assert kwargs["anim_count"] == "few"
    # force/build_extras are still set by recut_and_rederive itself (not overridden).
    assert kwargs["force"] is True
    assert kwargs["build_extras"] is False


def test_recut_and_rederive_none_anim_opts_is_fine(rederive_calls):
    result = pl.recut_and_rederive("8_short", "acorta", anim_opts=None)
    _name, kwargs = rederive_calls[1]
    # No anim_opts -> only force/build_extras are passed.
    assert kwargs == {"video_id": "8_short", "force": True, "build_extras": False}
    assert result is not None
