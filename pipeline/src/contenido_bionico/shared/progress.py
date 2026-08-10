"""Run progress and heartbeat helpers.

The LLM never writes progress files directly. Pipeline code calls this module
around long-running helper calls so an external assistant can check whether a
run is still healthy without interrupting subprocesses.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from contenido_bionico.shared import config


HEARTBEAT_INTERVAL_SECONDS = 60
STDOUT_HEARTBEAT_SECONDS = 5 * 60
NORMAL_JOB_MINUTES = 20
STALE_AFTER_MINUTES = 25

_LOCK = threading.Lock()


def runs_root() -> Path:
    return config.REPO_ROOT / "runs"


def run_dir(run_id: int | str) -> Path:
    return runs_root() / str(run_id)


def logs_dir(run_id: int | str) -> Path:
    return run_dir(run_id) / "logs"


def status_path(run_id: int | str) -> Path:
    return logs_dir(run_id) / "status.json"


def events_path(run_id: int | str) -> Path:
    return logs_dir(run_id) / "events.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None = None) -> str:
    return (ts or _now()).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read_status_unlocked(run_id: int | str) -> dict[str, Any]:
    path = status_path(run_id)
    if not path.exists():
        return {
            "run_id": str(run_id),
            "status": "unknown",
            "active_jobs": [],
            "completed_jobs": 0,
            "failed_jobs": 0,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {
            "run_id": str(run_id),
            "status": "unreadable",
            "active_jobs": [],
            "completed_jobs": 0,
            "failed_jobs": 0,
        }
    return data if isinstance(data, dict) else {"run_id": str(run_id), "status": "invalid"}


def read_status(run_id: int | str) -> dict[str, Any]:
    with _LOCK:
        return _read_status_unlocked(run_id)


def _write_status_unlocked(run_id: int | str, status: dict[str, Any]) -> None:
    logs = logs_dir(run_id)
    logs.mkdir(parents=True, exist_ok=True)
    status["run_id"] = str(run_id)
    status["updated_at"] = _iso()
    # Write to a temp file in the same directory, then os.replace: atomic on
    # POSIX and Windows, so the cross-process `status` reader never sees a
    # half-written file.
    payload = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(logs),
            prefix=".status-",
            suffix=".tmp",
            delete=False,
        ) as fp:
            tmp_name = fp.name
            fp.write(payload)
        os.replace(tmp_name, status_path(run_id))
    except BaseException:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def append_event(run_id: int | str, event: dict[str, Any]) -> None:
    record = {"timestamp": _iso(), **event}
    with _LOCK:
        logs = logs_dir(run_id)
        logs.mkdir(parents=True, exist_ok=True)
        with events_path(run_id).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def update_status(run_id: int | str, **updates: Any) -> dict[str, Any]:
    with _LOCK:
        status = _read_status_unlocked(run_id)
        if status.get("status") == "unreadable":
            # A concurrent writer may explain the bad read; retry once. If the
            # file is still unreadable, never persist the fallback dict over
            # it: return the merged view without writing.
            status = _read_status_unlocked(run_id)
            if status.get("status") == "unreadable":
                return {**status, **updates}
        status.update(updates)
        _write_status_unlocked(run_id, status)
        return status


def start_run(
    run_id: int | str,
    *,
    pipeline: str,
    stage: str,
    max_concurrency: int | None = None,
    total_jobs: int | None = None,
) -> None:
    status = {
        "run_id": str(run_id),
        "status": "running",
        "pipeline": pipeline,
        "stage": stage,
        "max_concurrency": max_concurrency,
        "total_jobs": total_jobs,
        "active_jobs": [],
        "completed_jobs": 0,
        "failed_jobs": 0,
        "message": (
            "Animation jobs can take 10-20 minutes each. "
            "Do not stop or relaunch unless the heartbeat is stale."
        ),
        "started_at": _iso(),
        "last_heartbeat_at": _iso(),
        "stale_after_minutes": STALE_AFTER_MINUTES,
    }
    with _LOCK:
        _write_status_unlocked(run_id, status)
    append_event(run_id, {"type": "run_start", "pipeline": pipeline, "stage": stage})


def set_stage(run_id: int | str, stage: str, **updates: Any) -> None:
    updates["stage"] = stage
    update_status(run_id, **updates)
    append_event(run_id, {"type": "stage", "stage": stage, **updates})


def finish_run(run_id: int | str, *, status: str, message: str = "") -> None:
    now = _iso()
    current = read_status(run_id)
    update_status(
        run_id,
        status=status,
        active_jobs=[],
        finished_at=now,
        last_heartbeat_at=now,
        message=message or current.get("message", ""),
    )
    append_event(run_id, {"type": "run_finish", "status": status, "message": message})


def _job_id(kind: str, segment_id: int | None, attempt: int | None) -> str:
    parts = [kind]
    if segment_id is not None:
        parts.append(f"seg-{segment_id}")
    if attempt is not None:
        parts.append(f"attempt-{attempt}")
    return ":".join(parts)


def _elapsed_seconds(started_at: str) -> float:
    start = _parse_iso(started_at)
    if start is None:
        return 0.0
    return max(0.0, (_now() - start).total_seconds())


def _read_status_for_write_unlocked(run_id: int | str) -> dict[str, Any] | None:
    """Current status for read-modify-write, or None when it must not be written.

    Same never-persist-the-fallback guard as update_status: a concurrent
    writer may explain a bad read; retry once, and if the file is still
    unreadable, refuse to overwrite it with the fallback dict.
    """
    status = _read_status_unlocked(run_id)
    if status.get("status") == "unreadable":
        status = _read_status_unlocked(run_id)
        if status.get("status") == "unreadable":
            return None
    return status


def _upsert_job_unlocked(run_id: int | str, job: dict[str, Any]) -> None:
    status = _read_status_for_write_unlocked(run_id)
    if status is None:
        return
    jobs = [
        item for item in status.get("active_jobs", [])
        if isinstance(item, dict) and item.get("job_id") != job["job_id"]
    ]
    jobs.append(job)
    status["active_jobs"] = jobs
    status["last_heartbeat_at"] = job["last_heartbeat_at"]
    _write_status_unlocked(run_id, status)


def _finish_job_unlocked(run_id: int | str, job_id: str, success: bool, message: str) -> None:
    status = _read_status_for_write_unlocked(run_id)
    if status is None:
        return
    jobs = [
        item for item in status.get("active_jobs", [])
        if isinstance(item, dict) and item.get("job_id") != job_id
    ]
    status["active_jobs"] = jobs
    counter = "completed_jobs" if success else "failed_jobs"
    status[counter] = int(status.get(counter) or 0) + 1
    status["last_heartbeat_at"] = _iso()
    if message:
        status["message"] = message
    _write_status_unlocked(run_id, status)


@contextmanager
def job(
    run_id: int | str,
    *,
    kind: str,
    stage: str,
    segment_id: int | None = None,
    attempt: int | None = None,
    message: str = "",
    normal_minutes: int = NORMAL_JOB_MINUTES,
    stale_after_minutes: int = STALE_AFTER_MINUTES,
) -> Iterator[None]:
    """Track one long-running operation.

    A daemon heartbeat updates status.json while the wrapped operation blocks.
    This is intentionally implemented in Python so the LLM does not spend
    tokens writing progress reports.
    """
    started_at = _iso()
    job_id = _job_id(kind, segment_id, attempt)
    stop = threading.Event()
    last_stdout = 0.0
    job_record = {
        "job_id": job_id,
        "kind": kind,
        "stage": stage,
        "segment_id": segment_id,
        "attempt": attempt,
        "started_at": started_at,
        "last_heartbeat_at": started_at,
        "elapsed_seconds": 0.0,
        "normal_minutes": normal_minutes,
        "stale_after_minutes": stale_after_minutes,
        "message": message,
    }
    with _LOCK:
        _upsert_job_unlocked(run_id, job_record)
    append_event(run_id, {"type": "job_start", **job_record})

    def heartbeat() -> None:
        nonlocal last_stdout
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            now = _iso()
            elapsed = _elapsed_seconds(started_at)
            job_record["last_heartbeat_at"] = now
            job_record["elapsed_seconds"] = round(elapsed, 3)
            with _LOCK:
                _upsert_job_unlocked(run_id, dict(job_record))
            append_event(run_id, {"type": "heartbeat", **job_record})
            if elapsed - last_stdout >= STDOUT_HEARTBEAT_SECONDS:
                last_stdout = elapsed
                minutes = elapsed / 60.0
                print(
                    f"[bionico] still working: {kind}"
                    f"{' seg=' + str(segment_id) if segment_id is not None else ''}, "
                    f"elapsed {minutes:.1f} min. Normal range: 10-20 min per animation.",
                    flush=True,
                )

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    success = False
    try:
        yield
        success = True
    finally:
        stop.set()
        elapsed = _elapsed_seconds(started_at)
        with _LOCK:
            _finish_job_unlocked(
                run_id,
                job_id,
                success,
                message or (
                    "Job completed." if success else "Job failed or was interrupted."
                ),
            )
        append_event(
            run_id,
            {
                "type": "job_finish",
                "job_id": job_id,
                "kind": kind,
                "stage": stage,
                "segment_id": segment_id,
                "attempt": attempt,
                "elapsed_seconds": round(elapsed, 3),
                "status": "SUCCESS" if success else "FAILED",
            },
        )


def _latest_run_id() -> str | None:
    root = runs_root()
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and p.name.split("_")[0].isdigit()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).name


def resolve_status_run_id(value: str | None) -> str | None:
    if value:
        if run_dir(value).is_dir():
            return value
        # A bare number ("5") refers to a visible run folder ("5_long"/"5_short").
        if str(value).isdigit():
            matches = [
                f"{value}_{kind}"
                for kind in ("long", "short")
                if run_dir(f"{value}_{kind}").is_dir()
            ]
            if len(matches) == 1:
                return matches[0]
        return value
    return _latest_run_id()


def status_summary(run_id: int | str) -> dict[str, Any]:
    status = read_status(run_id)
    active = [j for j in status.get("active_jobs", []) if isinstance(j, dict)]
    now = _now()
    for job_item in active:
        started = _parse_iso(job_item.get("started_at"))
        last = _parse_iso(job_item.get("last_heartbeat_at"))
        if started is not None:
            job_item["elapsed_seconds"] = round(max(0.0, (now - started).total_seconds()), 3)
        if last is not None:
            job_item["heartbeat_age_seconds"] = round(max(0.0, (now - last).total_seconds()), 3)
    status["active_jobs"] = active
    last_run_heartbeat = _parse_iso(status.get("last_heartbeat_at"))
    age = max(0.0, (now - last_run_heartbeat).total_seconds()) if last_run_heartbeat else None
    status["heartbeat_age_seconds"] = round(age, 3) if age is not None else None
    stale_after = float(status.get("stale_after_minutes") or STALE_AFTER_MINUTES) * 60.0
    status["heartbeat_stale"] = bool(age is not None and age > stale_after)
    return status


def format_status(status: dict[str, Any]) -> str:
    run_id = status.get("run_id", "?")
    state = status.get("status", "unknown")
    stage = status.get("stage", "unknown")
    active = [j for j in status.get("active_jobs", []) if isinstance(j, dict)]
    max_concurrency = status.get("max_concurrency")
    lines = [
        f"Run {run_id}: {state}",
        f"Stage: {stage}",
        f"Active jobs: {len(active)}"
        + (f" (max concurrency {max_concurrency})" if max_concurrency else ""),
    ]
    if active:
        longest = max(active, key=lambda item: float(item.get("elapsed_seconds") or 0.0))
        elapsed_min = float(longest.get("elapsed_seconds") or 0.0) / 60.0
        seg = longest.get("segment_id")
        seg_text = f" segment {seg}" if seg is not None else ""
        lines.append(
            f"Longest job: {longest.get('kind', 'job')}{seg_text}, {elapsed_min:.1f} min"
        )
    age = status.get("heartbeat_age_seconds")
    if isinstance(age, (int, float)):
        lines.append(f"Heartbeat age: {age:.0f}s")
    if status.get("heartbeat_stale"):
        lines.append(
            "Status: stale heartbeat. Ask the user before stopping or relaunching anything."
        )
    else:
        lines.append(
            "Status: working/healthy. Animation can take 10-20 minutes per scene; do not stop or relaunch."
        )
    message = status.get("message")
    if message:
        lines.append(f"Note: {message}")
    return "\n".join(lines)


def print_status(run_id: str | None, *, as_json: bool = False) -> int:
    resolved = resolve_status_run_id(run_id)
    if resolved is None:
        print("No encontre runs para revisar.")
        return 1
    if not run_dir(resolved).is_dir():
        # Never fabricate a healthy-looking status for a run that does not exist.
        print(
            f"No existe ningun run con id {resolved}. Revisa el id, o corre "
            "`contenido-bionico status` sin id para ver el run mas reciente."
        )
        return 1
    status = status_summary(resolved)
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(format_status(status))
    return 0
