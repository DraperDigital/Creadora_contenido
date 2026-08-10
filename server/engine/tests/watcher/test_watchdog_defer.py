import json
import sys
from pathlib import Path

from bionico.watcher import jobrunner
from bionico.watcher import watcher as w


# --- watchdog ------------------------------------------------------------------

def test_watchdog_ceiling_trips_on_wall_clock():
    info = {"started_ts": 1000.0, "job_log": None}
    now = 1000.0 + w.JOB_CEILING_SECONDS + 1
    reason = w.watchdog_reason(info, now)
    assert reason and "tiempo maximo" in reason


def test_watchdog_healthy_job_within_ceiling_is_none(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("working\n", encoding="utf-8")
    info = {"started_ts": 1000.0, "job_log": str(log)}
    assert w.watchdog_reason(info, 1000.0 + 60) is None


def test_watchdog_stall_trips_when_log_stops_growing(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("some output\n", encoding="utf-8")
    info = {"started_ts": 1000.0, "job_log": str(log)}
    # First check records the size; still healthy.
    assert w.watchdog_reason(info, 1000.0) is None
    # Same size, past the stall window.
    now = 1000.0 + w.STALL_SECONDS + 1
    reason = w.watchdog_reason(info, now)
    assert reason and "sin actividad" in reason


def test_watchdog_log_growth_resets_stall_clock(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("a\n", encoding="utf-8")
    info = {"started_ts": 1000.0, "job_log": str(log)}
    assert w.watchdog_reason(info, 1000.0) is None
    log.write_text("a\nb\n", encoding="utf-8")  # grew right before the window
    now = 1000.0 + w.STALL_SECONDS + 1
    assert w.watchdog_reason(info, now) is None  # growth observed -> clock reset


# --- quota defer (exit code 75) --------------------------------------------------

def test_finish_defer_requeues_without_consuming_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_quota_defer_until", lambda now: now + 999)
    led = {"bionico/q1": {"status": "running", "attempts": 1, "mp4": None, "json": None}}
    w.finish(led, "bionico/q1", "defer", "limite de uso", None,
             tmp_path / "ledger.json")
    rec = led["bionico/q1"]
    assert rec["status"] == "pending"
    assert rec["attempts"] == 0          # the quota defer did NOT consume the retry
    assert rec["defer_until"] > 0
    assert rec["quota"] is True


def test_next_pending_skips_deferred_until_reset(tmp_path):
    led = {
        "bionico/deferred": {"status": "pending", "mtime": 1, "defer_until": 2000.0},
        "bionico/ready": {"status": "pending", "mtime": 2},
    }
    assert w.next_pending(led, now=1000.0) == "bionico/ready"
    assert w.next_pending(led, now=3000.0) == "bionico/deferred"  # older mtime wins


def test_finish_from_result_maps_quota_to_defer(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_quota_defer_until", lambda now: now + 999)
    led = {"bionico/q2": {"status": "running", "attempts": 1, "mp4": None, "json": None}}
    w._finish_from_result(led, "bionico/q2", {"rc": jobrunner.EXIT_QUOTA},
                          None, tmp_path / "ledger.json")
    assert led["bionico/q2"]["status"] == "pending"
    assert led["bionico/q2"]["quota"] is True


def test_finish_from_result_permanent_skips_retry(tmp_path):
    led = {"bionico/p1": {"status": "running", "attempts": 1, "mp4": None, "json": None}}
    w._finish_from_result(led, "bionico/p1", {"rc": 2, "permanent": True},
                          None, tmp_path / "ledger.json")
    # attempts 1 < MAX_ATTEMPTS would normally retry; permanent terminalizes.
    assert led["bionico/p1"]["status"] == "failed"


def test_jobrunner_maps_limit_marker_to_exit_quota(tmp_path):
    result = tmp_path / "jobs" / "r.result.json"
    # A child that writes the limit marker (like claude_runner does) and fails.
    code = ("import os,sys; open(os.environ['BIONICO_LIMIT_MARKER'],'w').close(); "
            "sys.exit(1)")
    rc = jobrunner.run("bionico/x", json.dumps(["-c", code]), str(tmp_path),
                       sys.executable, str(result))
    assert rc == jobrunner.EXIT_QUOTA
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["rc"] == jobrunner.EXIT_QUOTA
    assert data["quota"] is True
    assert not result.with_name(result.name + ".limit").exists()  # marker cleaned


def test_jobrunner_success_has_no_quota_flag(tmp_path):
    result = tmp_path / "jobs" / "ok.result.json"
    rc = jobrunner.run("bionico/y", json.dumps(["-c", "pass"]), str(tmp_path),
                       sys.executable, str(result))
    assert rc == 0
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["rc"] == 0 and data["quota"] is False and data["permanent"] is False


# --- cloud inputs survive until publish -------------------------------------------

def _cloud_rec(inbox: Path, stem: str):
    mp4 = inbox / (stem + ".mp4")
    mp4.write_bytes(b"vid")
    sidecar = inbox / (stem + ".json")
    sidecar.write_text(json.dumps({"source": "cloud", "id": stem}), encoding="utf-8")
    return {"status": "running", "attempts": 1, "mp4": str(mp4), "json": str(sidecar)}


def test_finish_ok_keeps_cloud_input_for_publish(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    led = {"bionico/c1": _cloud_rec(inbox, "c1")}
    w.finish(led, "bionico/c1", "ok", "ok", None, tmp_path / "ledger.json")
    assert led["bionico/c1"]["status"] == "done"
    assert (inbox / "c1.mp4").exists()   # the agent deletes it AFTER publishing
    assert (inbox / "c1.json").exists()


def test_finish_ok_drops_non_cloud_input(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    mp4 = inbox / "m1.mp4"
    mp4.write_bytes(b"vid")
    sidecar = inbox / "m1.json"
    sidecar.write_text(json.dumps({"format": "short", "id": "m1"}), encoding="utf-8")
    led = {"bionico/m1": {"status": "running", "attempts": 1,
                          "mp4": str(mp4), "json": str(sidecar)}}
    w.finish(led, "bionico/m1", "ok", "ok", None, tmp_path / "ledger.json")
    assert not mp4.exists()  # legacy/manual drops keep the old cleanup behavior
