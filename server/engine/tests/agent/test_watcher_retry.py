import json
from pathlib import Path

from bionico.watcher import watcher as w


def _drop(inbox: Path, stem: str, content: bytes) -> Path:
    mp4 = inbox / (stem + ".mp4")
    mp4.write_bytes(content)
    return mp4


def test_failed_record_requeues_on_same_content_redrop(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger_path = tmp_path / "_work" / "ledger.json"
    mp4 = _drop(inbox, "vid1", b"SAME BYTES")
    led = {}
    w.scan_and_enqueue(led, inbox, ledger_path)
    key = "bionico/vid1"
    assert led[key]["status"] == "pending"
    led[key]["status"] = "failed"
    led[key]["attempts"] = 2
    mp4.unlink()  # watcher leaves failed inputs, but Retry re-drops a fresh copy
    _drop(inbox, "vid1", b"SAME BYTES")
    w.scan_and_enqueue(led, inbox, ledger_path)
    assert led[key]["status"] == "pending"
    assert led[key]["attempts"] == 0


def test_done_record_still_skipped_on_same_content_redrop(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger_path = tmp_path / "_work" / "ledger.json"
    mp4 = _drop(inbox, "vid2", b"SAME BYTES")
    led = {}
    w.scan_and_enqueue(led, inbox, ledger_path)
    key = "bionico/vid2"
    led[key]["status"] = "done"
    mp4.unlink()
    _drop(inbox, "vid2", b"SAME BYTES")
    w.scan_and_enqueue(led, inbox, ledger_path)
    assert led[key]["status"] == "done"


def test_reconcile_requeues_crashed_job_below_cap(tmp_path):
    # A 'running' job whose process died with no result file (e.g. the whole
    # tree was killed so jobrunner's finally never wrote a result) must NOT
    # requeue forever: below the attempt cap it retries once.
    work_dir = tmp_path / "_work"
    (work_dir / "jobs").mkdir(parents=True)
    ledger_path = work_dir / "ledger.json"
    key = "bionico/vid3"
    led = {key: {"status": "running", "attempts": 1, "pid": None, "mp4": None}}
    running = w.reconcile(led, work_dir, ledger_path)
    assert key not in running
    assert led[key]["status"] == "pending"          # attempts 1 < MAX_ATTEMPTS -> retry


def test_reconcile_fails_crashed_job_at_cap(tmp_path):
    # At the attempt cap the same crashed job terminalizes as 'failed' (with a
    # human detail) instead of requeuing forever -- this is what the pull agent
    # relays to the cloud so the dashboard stops showing "processing".
    work_dir = tmp_path / "_work"
    (work_dir / "jobs").mkdir(parents=True)
    ledger_path = work_dir / "ledger.json"
    key = "bionico/vid4"
    led = {key: {"status": "running", "attempts": w.MAX_ATTEMPTS, "pid": None, "mp4": None}}
    running = w.reconcile(led, work_dir, ledger_path)
    assert key not in running
    assert led[key]["status"] == "failed"           # attempts == MAX -> terminalize
    assert "sin escribir un resultado" in led[key]["detail"]
