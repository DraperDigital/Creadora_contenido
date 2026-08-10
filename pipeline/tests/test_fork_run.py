"""Unit tests for the versioned-edit run fork + per-version disk reclaim, and
the build_extras skip a forked video edit uses to reuse the parent's extras.

fork_run/reclaim_run touch only the runs/ and output_short/ trees, so the tests
monkeypatch RUNS_DIR + config.configured_output_dir onto tmp_path — no Remotion
or agent ever runs.
"""
import contenido_bionico.pipeline as pl


def _mk(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_fork_run_copies_intermediates_minus_logs_and_output(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    out_base = tmp_path / "output_short"
    monkeypatch.setattr(pl, "RUNS_DIR", runs)
    monkeypatch.setattr(
        pl.config, "configured_output_dir",
        lambda kind: out_base if kind == "short" else None,
    )

    # Parent run 1_short: intermediates + transcript, plus a logs tree that must
    # NOT be forked (so the new version's token sum stays self-contained).
    _mk(runs / "1_short" / "transcript.json", b"T")
    _mk(runs / "1_short" / "_intermediates" / "plan.json", b"P")
    _mk(runs / "1_short" / "logs" / "author.stream.jsonl", b'{"type":"result"}')
    # Parent output folder run_1: the numbered final video + kept extras.
    _mk(out_base / "run_1" / "final_1.mp4", b"V")
    _mk(out_base / "run_1" / "quote_1.mp4", b"Q")
    _mk(out_base / "run_1" / "carousel.json", b"C")

    new_id = pl.fork_run("1_short")
    assert new_id == "2_short"  # lowest free short number

    # Run dir forked WITHOUT logs/.
    assert (runs / "2_short" / "transcript.json").read_bytes() == b"T"
    assert (runs / "2_short" / "_intermediates" / "plan.json").read_bytes() == b"P"
    assert not (runs / "2_short" / "logs").exists()

    # Output folder forked; the final was renamed to the new run number so a
    # video edit overwrites it in place; the extras were carried unchanged.
    assert (out_base / "run_2" / "final_2.mp4").read_bytes() == b"V"
    assert not (out_base / "run_2" / "final_1.mp4").exists()
    assert (out_base / "run_2" / "quote_1.mp4").exists()
    assert (out_base / "run_2" / "carousel.json").exists()

    # Parent version fully intact (its logs + numbered final still present).
    assert (runs / "1_short" / "logs" / "author.stream.jsonl").exists()
    assert (out_base / "run_1" / "final_1.mp4").exists()


def test_fork_run_without_output_folder_still_forks_run(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    monkeypatch.setattr(pl, "RUNS_DIR", runs)
    monkeypatch.setattr(pl.config, "configured_output_dir", lambda kind: None)
    _mk(runs / "1_short" / "transcript.json", b"T")
    new_id = pl.fork_run("1_short")
    assert new_id == "2_short"
    assert (runs / "2_short" / "transcript.json").read_bytes() == b"T"


def test_reclaim_run_removes_run_and_output(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    out_base = tmp_path / "output_short"
    monkeypatch.setattr(pl, "RUNS_DIR", runs)
    monkeypatch.setattr(pl.config, "configured_output_dir", lambda kind: out_base)
    _mk(runs / "5_short" / "transcript.json")
    _mk(out_base / "run_5" / "final_5.mp4")
    _mk(out_base / "run_5" / "quote_1.mp4")

    pl.reclaim_run("5_short")
    assert not (runs / "5_short").exists()
    assert not (out_base / "run_5").exists()


def test_reclaim_run_leaves_siblings_untouched(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    out_base = tmp_path / "output_short"
    monkeypatch.setattr(pl, "RUNS_DIR", runs)
    monkeypatch.setattr(pl.config, "configured_output_dir", lambda kind: out_base)
    _mk(runs / "5_short" / "a.json")
    _mk(runs / "6_short" / "a.json")
    _mk(out_base / "run_5" / "final_5.mp4")
    _mk(out_base / "run_6" / "final_6.mp4")

    pl.reclaim_run("5_short")
    assert not (runs / "5_short").exists()
    assert (runs / "6_short" / "a.json").exists()
    assert (out_base / "run_6" / "final_6.mp4").exists()


def test_reclaim_run_missing_is_noop(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    monkeypatch.setattr(pl, "RUNS_DIR", runs)
    monkeypatch.setattr(pl.config, "configured_output_dir", lambda kind: tmp_path / "nope")
    # Nothing to remove — must return quietly, never raise.
    pl.reclaim_run("99_short")


def test_run_short_phase_build_extras_false_skips_regeneration(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    monkeypatch.setattr(pl, "RUNS_DIR", runs)
    (runs / "1_short").mkdir(parents=True)
    (runs / "1_short" / "source.mp4").write_bytes(b"S")
    (runs / "1_short" / "transcript.json").write_bytes(b"T")
    monkeypatch.setattr(pl.short_animate_orchestrator, "main", lambda argv: 0)
    monkeypatch.setattr(pl, "publish_output", lambda *a, **k: runs / "1_short" / "final.mp4")
    monkeypatch.setattr(pl, "_prune_remotion_run_artifacts", lambda vid: None)
    calls = []
    monkeypatch.setattr(pl, "build_short_extras", lambda *a, **k: calls.append((a, k)))

    pl.run_short_phase("1_short", build_extras=False)
    assert calls == []  # a forked video edit reuses the parent's carousel/quotes

    pl.run_short_phase("1_short", build_extras=True)
    assert len(calls) == 1  # a normal short still builds its extras
