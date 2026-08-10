import subprocess
import sys
import time
from pathlib import Path

import psutil

from bionico.watcher import watcher


def test_kill_tree_kills_a_real_process():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert psutil.pid_exists(proc.pid)
        watcher._kill_tree(proc.pid)
        deadline = time.time() + 5
        while psutil.pid_exists(proc.pid) and time.time() < deadline:
            time.sleep(0.1)
        assert not psutil.pid_exists(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_kill_tree_on_dead_or_invalid_pid_is_a_noop():
    # A pid that cannot exist / isn't a valid int must not raise.
    watcher._kill_tree(2**31 - 1)
    watcher._kill_tree("not-a-pid")


def test_cancel_marker_path():
    marker = watcher._cancel_marker(Path("/work"), "bionico/abc123")
    assert marker == Path("/work") / "cancel" / "abc123.cancel"


def _mp4_rec(mp4: Path, status="pending"):
    return {"folder": "bionico", "id": mp4.stem, "mp4": str(mp4),
            "json": str(mp4.with_suffix(".json")), "status": status,
            "attempts": 0, "mtime": 0, "size": 0, "digest": "x",
            "added": "2026-01-01T00:00:00"}


def test_apply_pending_cancels_marks_canceled_and_drops_inbox(tmp_path):
    work_dir = tmp_path / "_work"
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "abc123.mp4"
    mp4.write_bytes(b"data")
    js = inbox / "abc123.json"
    js.write_text("{}", encoding="utf-8")

    led = {"bionico/abc123": _mp4_rec(mp4)}
    ledger_path = work_dir / "ledger.json"

    marker_dir = work_dir / "cancel"
    marker_dir.mkdir(parents=True)
    (marker_dir / "abc123.cancel").write_text("", encoding="utf-8")

    watcher.apply_pending_cancels(led, work_dir, ledger_path)

    rec = led["bionico/abc123"]
    assert rec["status"] == "canceled"
    assert rec["detail"] == "canceled by user"
    assert "finished" in rec
    assert not mp4.exists()
    assert not js.exists()
    assert not (marker_dir / "abc123.cancel").exists()
    assert ledger_path.exists()  # persisted


def test_apply_pending_cancels_ignores_running_and_no_marker(tmp_path):
    work_dir = tmp_path / "_work"
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "vid1.mp4"
    mp4.write_bytes(b"data")

    led = {
        "bionico/vid1": _mp4_rec(mp4, status="running"),
        "bionico/vid2": _mp4_rec(inbox / "vid2.mp4", status="pending"),
    }
    ledger_path = work_dir / "ledger.json"
    # No marker files dropped at all.
    watcher.apply_pending_cancels(led, work_dir, ledger_path)

    assert led["bionico/vid1"]["status"] == "running"
    assert led["bionico/vid2"]["status"] == "pending"
    assert not ledger_path.exists()  # nothing changed -> no save


def test_apply_pending_cancels_reaps_stale_marker_for_done_record(tmp_path):
    """A marker left over after the job already finished must be swept, but
    the (already terminal) record and any inbox leftovers stay untouched."""
    work_dir = tmp_path / "_work"
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "done1.mp4"
    mp4.write_bytes(b"data")

    led = {"bionico/done1": _mp4_rec(mp4, status="done")}
    ledger_path = work_dir / "ledger.json"

    marker_dir = work_dir / "cancel"
    marker_dir.mkdir(parents=True)
    (marker_dir / "done1.cancel").write_text("", encoding="utf-8")

    watcher.apply_pending_cancels(led, work_dir, ledger_path)

    assert led["bionico/done1"]["status"] == "done"
    assert "detail" not in led["bionico/done1"] or led["bionico/done1"].get("detail") != "canceled by user"
    assert mp4.exists()  # inbox file untouched
    assert not (marker_dir / "done1.cancel").exists()  # marker reaped


def test_apply_pending_cancels_reaps_marker_with_no_ledger_record(tmp_path):
    """A marker for an id that no longer has a ledger record at all must be
    unlinked without creating or touching any ledger entry."""
    work_dir = tmp_path / "_work"
    led = {}
    ledger_path = work_dir / "ledger.json"

    marker_dir = work_dir / "cancel"
    marker_dir.mkdir(parents=True)
    (marker_dir / "ghost1.cancel").write_text("", encoding="utf-8")

    watcher.apply_pending_cancels(led, work_dir, ledger_path)

    assert led == {}
    assert not (marker_dir / "ghost1.cancel").exists()
    assert not ledger_path.exists()  # nothing changed in the ledger -> no save


def test_apply_pending_cancels_leaves_marker_for_running_record(tmp_path):
    """A marker for a running record must be left in place for the
    running-jobs poll to consume; the record must not be touched here."""
    work_dir = tmp_path / "_work"
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "run1.mp4"
    mp4.write_bytes(b"data")

    led = {"bionico/run1": _mp4_rec(mp4, status="running")}
    ledger_path = work_dir / "ledger.json"

    marker_dir = work_dir / "cancel"
    marker_dir.mkdir(parents=True)
    (marker_dir / "run1.cancel").write_text("", encoding="utf-8")

    watcher.apply_pending_cancels(led, work_dir, ledger_path)

    assert led["bionico/run1"]["status"] == "running"
    assert (marker_dir / "run1.cancel").exists()  # marker left in place


def test_canceled_record_is_terminal_like_done_and_failed(tmp_path):
    """A canceled record must not be mistaken for actionable anywhere."""
    led = {"bionico/x": {"status": "canceled"}}
    # next_pending never picks it.
    assert watcher.next_pending(led) is None

    # scan_and_enqueue: same-digest re-drop of a canceled record must NOT
    # requeue on the failed-only retry branch.
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "x.mp4"
    mp4.write_bytes(b"SAME BYTES")
    led2 = {}
    watcher.scan_and_enqueue(led2, inbox, tmp_path / "ledger.json")
    led2["bionico/x"]["status"] = "canceled"
    import os
    st = mp4.stat()
    os.utime(mp4, (st.st_atime, st.st_mtime + 100))  # re-stamp, same bytes
    watcher.scan_and_enqueue(led2, inbox, tmp_path / "ledger.json")
    assert led2["bionico/x"]["status"] == "canceled"  # not requeued to pending
