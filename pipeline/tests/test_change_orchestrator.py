"""Unit tests for the autonomous change orchestrator.

The orchestrator no longer plans a fixed set of tool calls: it launches an
autonomous editor agent (a tool-enabled Claude-Code sub-session) that edits the
run's render inputs and re-renders the affected deliverable, then ends by printing
a `RESULT: {...}` line. These tests monkeypatch the agent launch + publish so they
exercise only this module's own logic: message building, RESULT parsing +
validation, the publish safety net, and the input guards.
"""
import types

import pytest

import contenido_bionico.short.change.orchestrator as co


# ---------- RESULT line parsing ----------

def test_parse_result_line_plain():
    out = 'did work\nRESULT: {"changed": ["video"], "summary": "listo", "unsupported": null}\n'
    assert co._parse_result_line(out) == {
        "changed": ["video"], "summary": "listo", "unsupported": None,
    }


def test_parse_result_line_takes_last_and_tolerates_fences_and_trailing_prose():
    out = (
        'RESULT: {"changed": [], "summary": "primero", "unsupported": "x"}\n'
        '```\nRESULT: {"changed": ["carousel"], "summary": "final", "unsupported": null}\n```\n'
        "trailing chatter after the json\n"
    )
    parsed = co._parse_result_line(out)
    assert parsed["changed"] == ["carousel"] and parsed["summary"] == "final"


def test_parse_result_line_falls_back_to_bare_json_object():
    out = 'no prefix here\n{"changed": ["quotes"], "summary": "s", "unsupported": null}\n'
    assert co._parse_result_line(out)["changed"] == ["quotes"]


def test_parse_result_line_raises_without_result():
    with pytest.raises(co.ChangeError):
        co._parse_result_line("the agent said nothing structured\n")


# ---------- RESULT normalization / validation ----------

def test_normalize_result_drops_unknown_deliverables_and_dedupes():
    res = co._normalize_result({
        "changed": ["video", "bogus", "VIDEO", "carousel"],
        "summary": "  hecho  ", "unsupported": "null",
    })
    assert res["changed"] == ["video", "carousel"]   # unknown dropped, dup collapsed
    assert res["summary"] == "hecho"
    assert res["unsupported"] is None                # "null" string -> None


def test_normalize_result_keeps_unsupported_reason():
    res = co._normalize_result({"changed": [], "summary": "", "unsupported": "no hay subtitulos"})
    assert res == {"changed": [], "summary": "", "unsupported": "no hay subtitulos"}


# ---------- run_change end-to-end (agent + publish stubbed) ----------

def _fake_result(stdout, returncode=0):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr="", command=[],
        start_ts=None, end_ts=None,
    )


def _stub_launch(monkeypatch, tmp_path, stdout, *, returncode=0, capture=None):
    # The real system prompt is read from disk (PROMPT_PATH exists in-repo). Only
    # the agent launch, run dir, context + publish are stubbed.
    monkeypatch.setattr(co, "inspect_run", lambda vid, target: {"target": target})
    monkeypatch.setattr(co, "resolve_agent_cmd", lambda: "claude")
    monkeypatch.setattr(co, "_run_dir", lambda vid: tmp_path / f"co_run_{vid}")

    def fake_run(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _fake_result(stdout, returncode)

    monkeypatch.setattr(co, "run_agent_code", fake_run)


def test_run_change_parses_result_and_publishes_video(monkeypatch, tmp_path):
    published = []
    _stub_launch(monkeypatch, tmp_path,
                 'RESULT: {"changed": ["video"], "summary": "azul", "unsupported": null}')
    monkeypatch.setattr(co, "_ensure_published", lambda vid, changed: published.append((vid, changed)))
    res = co.run_change("7_short", "video", "haz los subtitulos azules")
    assert res == {"changed": ["video"], "summary": "azul", "unsupported": None}
    assert published == [("7_short", ["video"])]


def test_run_change_passes_message_and_launch_params(monkeypatch, tmp_path):
    capture = {}
    _stub_launch(
        monkeypatch, tmp_path,
        'RESULT: {"changed": ["carousel"], "summary": "ok", "unsupported": null}',
        capture=capture,
    )
    monkeypatch.setattr(co, "_ensure_published", lambda vid, changed: None)
    co.run_change("7_short", "carousel", "titulos mas grandes",
                  anim_opts={"anim_quality": "high", "no_music": True})

    msg = capture["initial_message"]
    assert "<TARGET>\ncarousel\n</TARGET>" in msg
    assert "titulos mas grandes" in msg          # USER_CHANGE_REQUEST echoed
    assert "<RUN_DIR>" in msg and "<RUN_CONTEXT>" in msg
    assert '"anim_opts"' in msg                   # version snapshot folded into context
    # autonomous launch: full tools, opus/high, bypass permissions, editor cwd
    assert capture["tools"] == ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
    assert capture["model"] == "claude-opus-4-8"
    assert capture["effort"] == "high"
    assert capture["permission_mode"] == "bypassPermissions"
    assert capture["cwd"] == co.PIPELINE_DIR


def test_run_change_unsupported_change_reports_and_publishes_nothing(monkeypatch, tmp_path):
    published = []
    _stub_launch(
        monkeypatch, tmp_path,
        'RESULT: {"changed": [], "summary": "", "unsupported": "este video no tiene subtitulos"}',
    )
    monkeypatch.setattr(co, "_ensure_published", lambda vid, changed: published.append(changed))
    res = co.run_change("7_short", "video", "cambia el color de los subtitulos")
    assert res["changed"] == [] and res["unsupported"] == "este video no tiene subtitulos"
    assert published == [[]]                       # nothing to publish


def test_run_change_agent_failure_raises(monkeypatch, tmp_path):
    _stub_launch(monkeypatch, tmp_path, "boom", returncode=1)
    with pytest.raises(co.ChangeError):
        co.run_change("7_short", "video", "algo")


def test_run_change_timeout_raises_clear_error(monkeypatch, tmp_path):
    # The inner agent hitting the wall-clock cap raises subprocess.TimeoutExpired
    # (its process tree is killed). run_change must convert that into a clear,
    # human ChangeError so the failure surfaces on the dashboard as a message,
    # not a raw TimeoutExpired repr.
    import subprocess

    monkeypatch.setattr(co, "inspect_run", lambda vid, target: {"target": target})
    monkeypatch.setattr(co, "resolve_agent_cmd", lambda: "claude")
    monkeypatch.setattr(co, "_run_dir", lambda vid: tmp_path / f"co_run_{vid}")

    def boom(**kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=co.AGENT_TIMEOUT_SECONDS)

    monkeypatch.setattr(co, "run_agent_code", boom)
    with pytest.raises(co.ChangeError) as exc:
        co.run_change("7_short", "video", "algo")
    assert "limite" in str(exc.value)   # "excedio el limite de N minutos ..."


def test_run_change_timeout_recovers_fresh_published_video(monkeypatch, tmp_path):
    import json
    import subprocess

    run_dir = tmp_path / "co_run_7_short"
    out_dir = tmp_path / "out"
    out_file = out_dir / "final_7.mp4"
    monkeypatch.setattr(co, "inspect_run", lambda vid, target: {"target": target})
    monkeypatch.setattr(co, "resolve_agent_cmd", lambda: "claude")
    monkeypatch.setattr(co, "_run_dir", lambda vid: run_dir)

    def boom(**kwargs):
        out_dir.mkdir(parents=True)
        out_file.write_bytes(b"fresh mp4")
        report = run_dir / "logs" / "Publish_Report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({
            "video_id": "7_short",
            "kind": "short",
            "run_filename": "final.mp4",
            "published_path": str(out_file),
            "duration_seconds": 12.3,
            "status": "published",
        }), encoding="utf-8")
        raise subprocess.TimeoutExpired(cmd="claude", timeout=co.AGENT_TIMEOUT_SECONDS)

    monkeypatch.setattr(co, "run_agent_code", boom)
    res = co.run_change("7_short", "video", "algo")
    assert res["changed"] == ["video"]
    assert res["unsupported"] is None
    assert "Recuperado" in res["summary"]


def test_run_change_missing_result_recovers_fresh_published_video(monkeypatch, tmp_path):
    import json

    run_dir = tmp_path / "co_run_7_short"
    out_dir = tmp_path / "out"
    out_file = out_dir / "final_7.mp4"
    monkeypatch.setattr(co, "inspect_run", lambda vid, target: {"target": target})
    monkeypatch.setattr(co, "resolve_agent_cmd", lambda: "claude")
    monkeypatch.setattr(co, "_run_dir", lambda vid: run_dir)

    def no_result(**kwargs):
        out_dir.mkdir(parents=True)
        out_file.write_bytes(b"fresh mp4")
        report = run_dir / "logs" / "Publish_Report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({
            "video_id": "7_short",
            "kind": "short",
            "run_filename": "final.mp4",
            "published_path": str(out_file),
            "duration_seconds": 12.3,
            "status": "published",
        }), encoding="utf-8")
        return _fake_result("I'll wait for the monitor to notify me.")

    monkeypatch.setattr(co, "run_agent_code", no_result)
    res = co.run_change("7_short", "video", "algo")
    assert res["changed"] == ["video"]
    assert res["unsupported"] is None


def test_run_change_rejects_bad_target_and_empty_notes():
    with pytest.raises(co.ChangeError):
        co.run_change("7_short", "bogus", "x")
    with pytest.raises(co.ChangeError):
        co.run_change("7_short", "video", "   ")
