#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Watcher + worker LITE: vigila el inbox bionico y corre contenido-bionico.

Cola durable (ledger.json) con pool concurrente auto-dimensionado y limitado por
carga real del sistema (psutil). Cross-platform.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

from bionico import notify as _notify
from bionico.watcher.jobrunner import EXIT_QUOTA

MAX_ATTEMPTS = 2
TASK_SUFFIX = ".task.json"

# Hung-job watchdog. Wall-clock ceiling is the PRIMARY guard (log growth can
# be fooled by buffered writers); the stall check catches jobs that hang
# early with a silent log. 0 disables either check.
STALL_SECONDS = float(os.environ.get("BIONICO_STALL_MINUTES") or 30) * 60
JOB_CEILING_SECONDS = float(os.environ.get("BIONICO_JOB_CEILING_HOURS") or 3) * 3600

# How long a quota-deferred job waits when the usage API gives no reset time.
QUOTA_DEFER_FALLBACK_SECONDS = 30 * 60


def _auto_max_jobs():
    # One video at a time by default. The work is API-bound, not CPU-bound: each
    # edit already saturates the Anthropic session via the pipeline's adaptive
    # scene concurrency, so running videos in parallel only multiplies the API
    # load and trips 429/529. Raise WATCHER_MAX_JOBS only if you have headroom
    # (e.g. a higher API tier).
    return 1


MAX_JOBS = int(os.environ.get("WATCHER_MAX_JOBS") or _auto_max_jobs())
CPU_CEILING_PCT = float(os.environ.get("WATCHER_CPU_CEILING") or 80.0)
MIN_FREE_RAM_GB = float(os.environ.get("WATCHER_MIN_FREE_GB") or 2.0)

_state = {"work_dir": None}


def _work_dir() -> Path:
    return _state["work_dir"]


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    wd = _work_dir()
    if wd is None:
        return
    try:
        wd.mkdir(parents=True, exist_ok=True)
        with open(wd / "worker.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def cpu_percent():
    # Non-blocking system-wide percent since the previous call.
    return psutil.cpu_percent(interval=None)


def free_ram_gb():
    return psutil.virtual_memory().available / (1024 ** 3)


def load_ledger(ledger_path: Path):
    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_ledger(led, ledger_path: Path):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ledger_path)


def resolve_exe(basename):
    base = Path(sys.executable).parent
    cands = [base / "Scripts" / (basename + ".exe"), base / (basename + ".exe"),
             base / "bin" / basename, base / basename]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which(basename)


def build_args(rec) -> list[str]:
    """CLI args for one job: an explicit task override, or the mp4 default."""
    args = rec.get("args")
    if args:
        return [str(a) for a in args]
    return [str(rec["mp4"]), "--short"]


def _load_task(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("kind") == "edit" else None


_ANIM_QUALITIES = ("low", "mid", "high", "max")
_ANIM_COUNTS = ("few", "default", "max")
# Feature toggle -> pipeline CLI flag. The last five are the V6 multi-format
# family switches (extra videos, video-carousels, carousel styles, static
# images, text deliverables); like the rest, only an explicit False emits.
_FEATURE_FLAGS = (("captions", "--no-captions"), ("camera", "--no-camera"),
                   ("animations", "--no-animations"),
                   ("music", "--no-music"), ("sfx", "--no-sfx"),
                   ("carousel", "--no-carousel"), ("quotes", "--no-quotes"),
                   ("videos_extra", "--no-videos-extra"),
                   ("video_carruseles", "--no-video-carruseles"),
                   ("carruseles_extra", "--no-carruseles-extra"),
                   ("imagenes", "--no-imagenes"), ("posters", "--no-posters"),
                   ("textos", "--no-textos"))


def feature_flags_args(features: dict | None, anim_count: str | None) -> list[str]:
    """Map feature toggles + anim_count to pipeline CLI flags.

    Only an explicit False for a known feature key emits its --no-<name> flag;
    missing keys and True values emit nothing. anim_count only emits when it is
    one of the three known tiers.
    """
    args: list[str] = []
    feats = features if isinstance(features, dict) else {}
    for name, flag in _FEATURE_FLAGS:
        if feats.get(name) is False:
            args.append(flag)
    if anim_count in _ANIM_COUNTS:
        args += ["--anim-count", anim_count]
    return args


def task_args(task: dict) -> list[str] | None:
    """Map a dashboard change task to a --change-run invocation; None if invalid.

    Every change (video|quotes|carousel) now routes through the change
    orchestrator: an LLM agent reads the request and applies only what was asked
    to the opened deliverable. `run_id` is the parent run to base on; --change-run
    forks it (so the original version stays intact). For a video target the
    version's own quality + feature snapshot rides along so a re-animation keeps
    the video's structure (never resets its audio/toggles)."""
    run_id = str(task.get("run_id") or "").strip()
    instructions = str(task.get("instructions") or "").strip()
    target = task.get("target")
    if not run_id or not instructions or target not in ("video", "quotes", "carousel"):
        return None
    args = ["--change-run", run_id, "--target", str(target), "--notes=" + instructions]
    if target == "video":
        quality = task.get("anim_quality")
        if quality in _ANIM_QUALITIES:
            args += ["--anim-quality", quality]
        args += feature_flags_args(task.get("features"), task.get("anim_count"))
    return args


def next_pending(led, now=None):
    now = time.time() if now is None else now
    # Quota-deferred jobs (defer_until in the future) are pending but not
    # launchable yet: launching would just burn the Claude CLI against a
    # still-exhausted usage window.
    pend = [(k, r) for k, r in led.items()
            if r["status"] == "pending" and float(r.get("defer_until") or 0) <= now]
    if not pend:
        return None
    pend.sort(key=lambda kr: kr[1].get("mtime", 0))
    return pend[0][0]


def _digest(path: Path) -> str | None:
    """sha256 of the file's bytes: the STABLE identity used to dedup.

    Content, never mtime, decides whether a video is new or replaced. A copy,
    sync, restore or backup that only re-stamps mtime/size therefore never
    re-triggers an edit of an already-processed video.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _sidecar_quality(json_path) -> str | None:
    """Best-effort read of the anim_quality hint from a dropped <id>.json sidecar."""
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    q = data.get("anim_quality") if isinstance(data, dict) else None
    return q if q in _ANIM_QUALITIES else None


def _sidecar_features(json_path) -> dict:
    """Best-effort read of the feature-toggle booleans from a sidecar json.

    Only the known keys survive, coerced to bool; anything else (missing
    file, parse error, non-dict payload, unknown keys) contributes nothing.
    """
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # The agent nests the toggles under a "features" object (drop_into_inbox);
    # read them there, not at the sidecar's top level.
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, dict):
        return {}
    return {name: bool(feats[name]) for name, _ in _FEATURE_FLAGS if name in feats}


def _sidecar_anim_count(json_path) -> str | None:
    """Best-effort read of the anim_count tier hint from a dropped sidecar."""
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    c = data.get("anim_count") if isinstance(data, dict) else None
    return c if c in _ANIM_COUNTS else None


def scan_and_enqueue(led, inbox_bionico_dir: Path, ledger_path: Path = None, persist=True):
    dirty = False
    d = Path(inbox_bionico_dir)
    if not d.exists():
        return
    for mp4 in d.glob("*.mp4"):
        key = "bionico/%s" % mp4.stem
        try:
            st = mp4.stat()
        except OSError:
            continue
        mtime, size = st.st_mtime, st.st_size
        existing = led.get(key)
        if existing is not None:
            # In flight: never touch a queued or mid-edit job.
            if existing.get("status") in ("pending", "running"):
                continue
            # Cheap unchanged check: identical size AND mtime => same file, skip
            # without hashing. Keeps the per-tick rescan free in the common case.
            prev_size = existing.get("size")
            if (prev_size is not None and int(prev_size) == size
                    and abs(float(existing.get("mtime", -1)) - mtime) < 1e-6):
                continue
            # size/mtime moved: decide by CONTENT, not by the timestamp.
            digest = _digest(mp4)
            prev_digest = existing.get("digest")
            if prev_digest is None:
                # Legacy record predating content hashing. Adopt the current
                # bytes as its identity and keep its existing status: a bare
                # mtime/size move we cannot attribute to a real edit must not
                # re-animate an already-finished video.
                existing["digest"], existing["mtime"], existing["size"] = digest, mtime, size
                dirty = True
                continue
            if digest == prev_digest:
                # Same bytes, only re-stamped (copy/sync/restore/backup).
                if existing.get("status") == "failed":
                    # Retry re-drops the same bytes: a plain skip would make the
                    # dashboard's Retry a no-op (re-download, refuse, insta-fail
                    # again with the stale log). Requeue instead.
                    existing.update(
                        status="pending", attempts=0, mtime=mtime, size=size,
                        mp4=str(mp4), json=str(mp4.with_suffix(".json")),
                    )
                    if ledger_path is not None:
                        _result_path(Path(ledger_path).parent, key).unlink(missing_ok=True)
                    dirty = True
                    log("RETRY %s -> requeue (same content re-dropped after failure)" % key)
                    continue
                # Refresh the cached stat so we stop re-hashing; do NOT re-edit.
                existing["mtime"], existing["size"] = mtime, size
                dirty = True
                continue
            # Genuinely different content under the same id -> fresh edit.
            existing.update(
                folder="bionico", id=mp4.stem, mp4=str(mp4),
                json=str(mp4.with_suffix(".json")), status="pending",
                attempts=0, mtime=mtime, size=size, digest=digest,
                replaced=datetime.now().isoformat(timespec="seconds"),
            )
            # Stale args from the previous drop must not survive a re-drop
            # with a default sidecar (no quality/toggles -> no args at all).
            existing.pop("args", None)
            sidecar_json = mp4.with_suffix(".json")
            quality = _sidecar_quality(sidecar_json)
            extra = feature_flags_args(_sidecar_features(sidecar_json), _sidecar_anim_count(sidecar_json))
            if quality or extra:
                args = [str(mp4), "--short"]
                if quality:
                    args += ["--anim-quality", quality]
                args += extra
                existing["args"] = args
            # Drop the previous run's result file so reconcile/launch start clean.
            if ledger_path is not None:
                _result_path(Path(ledger_path).parent, key).unlink(missing_ok=True)
            dirty = True
            log("REPLACED %s -> requeue (content changed)" % key)
            continue
        led[key] = {"folder": "bionico", "id": mp4.stem, "mp4": str(mp4),
                    "json": str(mp4.with_suffix(".json")), "status": "pending",
                    "attempts": 0, "mtime": mtime, "size": size,
                    "digest": _digest(mp4),
                    "added": datetime.now().isoformat(timespec="seconds")}
        sidecar_json = mp4.with_suffix(".json")
        quality = _sidecar_quality(sidecar_json)
        extra = feature_flags_args(_sidecar_features(sidecar_json), _sidecar_anim_count(sidecar_json))
        if quality or extra:
            args = [str(mp4), "--short"]
            if quality:
                args += ["--anim-quality", quality]
            args += extra
            led[key]["args"] = args
        dirty = True
        log("ENQUEUE %s" % key)
    for tf in d.glob("*" + TASK_SUFFIX):
        key = "bionico/%s" % tf.name[: -len(TASK_SUFFIX)]
        try:
            st = tf.stat()
        except OSError:
            continue
        mtime, size = st.st_mtime, st.st_size
        existing = led.get(key)
        if existing is not None:
            if existing.get("status") in ("pending", "running"):
                continue
            prev_size = existing.get("size")
            if (prev_size is not None and int(prev_size) == size
                    and abs(float(existing.get("mtime", -1)) - mtime) < 1e-6):
                continue
            digest = _digest(tf)
            if digest == existing.get("digest"):
                if existing.get("status") == "failed" and existing.get("args"):
                    existing.update(status="pending", attempts=0, mtime=mtime,
                                    size=size, mp4=str(tf), json=str(tf))
                    if ledger_path is not None:
                        _result_path(Path(ledger_path).parent, key).unlink(missing_ok=True)
                    dirty = True
                    log("RETRY %s -> requeue (same edit re-dropped after failure)" % key)
                else:
                    existing["mtime"], existing["size"] = mtime, size
                    dirty = True
                continue
            # new bytes: a fresh edit request for the same job
            task = _load_task(tf)
            args = task_args(task) if task else None
            if args is None:
                existing.update(status="failed", attempts=0, mtime=mtime, size=size,
                                digest=digest, mp4=str(tf), json=str(tf),
                                detail="invalid task file")
                dirty = True
                log("FAILED %s: invalid task file" % key)
                continue
            existing.update(
                folder="bionico", id=tf.name[: -len(TASK_SUFFIX)], mp4=str(tf),
                json=str(tf), status="pending", attempts=0, mtime=mtime, size=size,
                digest=digest, args=args,
                replaced=datetime.now().isoformat(timespec="seconds"),
            )
            if ledger_path is not None:
                _result_path(Path(ledger_path).parent, key).unlink(missing_ok=True)
            dirty = True
            log("REPLACED %s -> requeue (new edit request)" % key)
            continue
        task = _load_task(tf)
        args = task_args(task) if task else None
        if args is None:
            led[key] = {"folder": "bionico", "id": tf.name[: -len(TASK_SUFFIX)],
                        "mp4": str(tf), "json": str(tf), "status": "failed",
                        "attempts": 0, "mtime": mtime, "size": size,
                        "digest": _digest(tf), "detail": "invalid task file",
                        "added": datetime.now().isoformat(timespec="seconds")}
            dirty = True
            log("FAILED %s: invalid task file" % key)
            continue
        led[key] = {"folder": "bionico", "id": tf.name[: -len(TASK_SUFFIX)],
                    "mp4": str(tf), "json": str(tf), "status": "pending",
                    "attempts": 0, "mtime": mtime, "size": size,
                    "digest": _digest(tf), "args": args,
                    "added": datetime.now().isoformat(timespec="seconds")}
        dirty = True
        log("ENQUEUE %s (edit: %s)" % (key, task.get("target")))
    if dirty and persist and ledger_path is not None:
        save_ledger(led, ledger_path)


def _drop_inbox(rec):
    """Remove the processed input from the inbox after a successful edit.

    The render lives in pipeline/output_short/run_<n>, so the inbox
    <id>.mp4/<id>.json is pure transient input: keeping it only wastes disk
    (200-300 MB each) and lets a later rescan re-trigger a fresh edit. Removed
    only on success; a failed file is left in place so a retry can re-read it.
    """
    for p in (rec.get("mp4"), rec.get("json")):
        if p:
            try:
                Path(p).unlink()
            except OSError:
                pass


def _is_cloud_input(rec) -> bool:
    """True when this record's input came from the cloud agent (its sidecar or
    task json says source=cloud). Cloud inputs must survive until the agent
    PUBLISHES the outputs (the agent deletes them then): dropping them at
    'done' made a later publish-retry hit a missing file."""
    path = rec.get("json")
    if not path:
        return False
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("source") == "cloud"


def _parse_iso_epoch(value) -> float | None:
    """ISO timestamp -> epoch. Aware values ('Z'/offset) convert exactly;
    naive values (the ledger's local `started` stamps) are taken as LOCAL
    time, which is what datetime.timestamp() does for naive datetimes."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _quota_defer_until(now: float) -> float:
    """Epoch until which a quota-deferred job should wait: the five-hour
    window's resets_at when the usage API answers, else a fixed fallback."""
    try:
        # Lazy import: agent.py imports this module at load time.
        from bionico.agent.agent import read_usage_metrics
        metrics = read_usage_metrics() or {}
        ts = _parse_iso_epoch(metrics.get("five_hour_resets_at"))
        if ts is not None and ts > now:
            return min(ts, now + 6 * 3600)  # sanity ceiling
    except Exception:
        pass
    return now + QUOTA_DEFER_FALLBACK_SECONDS


def finish(led, key, outcome, detail, job_log, ledger_path: Path = None, persist=True,
           permanent=False):
    rec = led[key]
    rec["detail"] = detail
    if job_log:
        rec["job_log"] = str(job_log)
    if outcome == "defer":
        # Claude usage limit: requeue WITHOUT consuming a retry and wait for
        # the quota window to reset (interface: pipeline exit code 75).
        rec["status"] = "pending"
        rec["attempts"] = max(0, int(rec.get("attempts") or 1) - 1)
        rec["defer_until"] = _quota_defer_until(time.time())
        rec["quota"] = True
        log("DEFER %s (quota): %s" % (key, detail))
    elif outcome == "ok":
        rec["finished"] = datetime.now().isoformat(timespec="seconds")
        rec["status"] = "done"
        if not _is_cloud_input(rec):
            _drop_inbox(rec)
        log("DONE %s (%s)" % (key, detail))
    elif outcome == "fail" and not permanent and rec["attempts"] < MAX_ATTEMPTS:
        rec["finished"] = datetime.now().isoformat(timespec="seconds")
        rec["status"] = "pending"
        log("RETRY %s (attempt %d/%d): %s" % (key, rec["attempts"], MAX_ATTEMPTS, detail))
    else:
        rec["finished"] = datetime.now().isoformat(timespec="seconds")
        rec["status"] = "failed"
        log("FAILED %s: %s" % (key, detail))
        _notify.notify("Trabajo fallido", "%s: %s" % (key, detail))
    if persist and ledger_path is not None:
        save_ledger(led, ledger_path)


def _result_path(work_dir: Path, key: str) -> Path:
    return Path(work_dir) / "jobs" / (key.replace("/", "_") + ".result.json")


def _read_result(result: Path) -> dict:
    """Jobrunner result payload: at least {'rc': int}; may carry 'quota'
    (usage-limit deferral) and 'permanent' (deterministic input error)."""
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"rc": 1}
        data["rc"] = int(data.get("rc", 1))
        return data
    except (OSError, ValueError, TypeError):
        return {"rc": 1}


def _finish_from_result(led, key, res: dict, job_log, ledger_path: Path,
                        persist=True) -> None:
    """Map one jobrunner result onto finish(): ok / quota-defer / fail."""
    rc = res.get("rc", 1)
    if rc == 0:
        finish(led, key, "ok", "ok", job_log, ledger_path, persist=persist)
    elif rc == EXIT_QUOTA or res.get("quota"):
        finish(led, key, "defer",
               "limite de uso de Claude alcanzado; se reintentara al reiniciarse la cuota",
               job_log, ledger_path, persist=persist)
    else:
        finish(led, key, "fail", "CLI exit code %s" % rc, job_log, ledger_path,
               persist=persist, permanent=bool(res.get("permanent")))


def _pid_alive(pid) -> bool:
    try:
        return pid is not None and psutil.pid_exists(int(pid))
    except (TypeError, ValueError):
        return False


def _cancel_marker(work_dir: Path, key: str) -> Path:
    """Path to the cancel marker for a ledger key (e.g. 'bionico/<id>')."""
    return Path(work_dir) / "cancel" / (key.split("/", 1)[1] + ".cancel")


def watchdog_reason(info: dict, now: float) -> str | None:
    """Why a running job should be killed, or None while it looks healthy.

    Primary: wall-clock ceiling (BIONICO_JOB_CEILING_HOURS, default 3h) --
    log growth can be fooled by buffered writers, elapsed time cannot.
    Secondary: the job log has not grown for BIONICO_STALL_MINUTES
    (default 30). Mutates `info` to track log size/last-growth time."""
    started = float(info.get("started_ts") or now)
    if JOB_CEILING_SECONDS > 0 and now - started > JOB_CEILING_SECONDS:
        return ("el trabajo supero el tiempo maximo de %.1f horas y fue detenido"
                % (JOB_CEILING_SECONDS / 3600))
    path = info.get("job_log")
    if not path:
        return None
    try:
        size = Path(path).stat().st_size
    except OSError:
        return None
    if size != info.get("log_size"):
        info["log_size"] = size
        info["log_grew_ts"] = now
        return None
    last_growth = float(info.get("log_grew_ts") or started)
    if STALL_SECONDS > 0 and now - last_growth > STALL_SECONDS:
        return ("sin actividad en el log por %d minutos; proceso detenido"
                % (STALL_SECONDS / 60))
    return None


def _kill_tree(pid: int) -> None:
    """Kill a process and all its descendants (the whole jobrunner tree)."""
    try:
        proc = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return
    children = proc.children(recursive=True)
    for p in [*children, proc]:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs([*children, proc], timeout=10)


def _spawn_detached(cmd, cwd, log_fp):
    """Spawn fully detached so the edit outlives a watcher/stack restart."""
    # UTF-8 all the way down: a cp1252-defaulted child once crashed while
    # printing the summary of an already-finished 26-minute change run.
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = dict(cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                  stdout=log_fp, stderr=subprocess.STDOUT)
    if sys.platform == "win32":
        # CREATE_NO_WINDOW | NEW_PROCESS_GROUP: a console-less (DETACHED) job
        # would make the pipeline CLI/ffmpeg/node children pop visible console
        # windows; a hidden console is inherited silently by the whole job tree.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def launch(key: str, rec: dict, pipeline_dir: Path, jobs_log_dir: Path, work_dir: Path):
    """Spawn the detached jobrunner for one edit. Returns (status, payload).

    The edit runs as an independent process that writes a result file on exit,
    so stopping/restarting the watcher neither kills it nor loses its outcome.
    """
    exe = resolve_exe("contenido-bionico")
    if not exe:
        return "nocli", "contenido-bionico not found (install not complete)"
    jobs_log_dir.mkdir(parents=True, exist_ok=True)
    job_log = jobs_log_dir / ("%s_bionico_%s.log"
                              % (datetime.now().strftime("%Y%m%d-%H%M%S"), Path(rec["mp4"]).stem))
    result = _result_path(work_dir, key)
    result.parent.mkdir(parents=True, exist_ok=True)
    if result.exists():
        result.unlink()  # clear a stale result from a previous attempt
    cmd = [sys.executable, "-m", "bionico.watcher.jobrunner",
           key, json.dumps(build_args(rec)), str(pipeline_dir), exe, str(result)]
    lf = open(job_log, "ab")
    try:
        p = _spawn_detached(cmd, pipeline_dir, lf)
    except Exception as exc:
        lf.close()
        return "fail", "launch error: %s" % exc
    lf.close()  # the detached child keeps its own handle; the parent's is not needed
    return "started", {"pid": p.pid, "job_log": job_log, "result": result,
                       "started_ts": time.time()}


def reconcile(led, work_dir: Path, ledger_path: Path):
    """Decide what to do, on (re)start, with jobs the ledger marks 'running'.

    - result file present -> the edit finished while we were down; finalize it.
    - process still alive  -> adopt it; keep running, do NOT relaunch.
    - gone with no result  -> crashed/rebooted; requeue as pending.

    Returns the in-memory map of adopted (still-running) jobs.
    """
    running = {}
    changed = False
    for key, rec in list(led.items()):
        if rec.get("status") != "running":
            continue
        result = _result_path(work_dir, key)
        if result.exists():
            _finish_from_result(led, key, _read_result(result), rec.get("job_log"),
                                ledger_path, persist=False)
            changed = True
        elif _pid_alive(rec.get("pid")):
            started_ts = _parse_iso_epoch(rec.get("started")) or time.time()
            running[key] = {"pid": rec["pid"], "result": result,
                            "job_log": rec.get("job_log"), "started_ts": started_ts}
            log("ADOPTED %s (pid %s still editing)" % (key, rec["pid"]))
        else:
            # Crashed/killed with no result written -- e.g. the whole process
            # tree (jobrunner included) was killed, so jobrunner's `finally`
            # never wrote the result file. Route through finish() so the retry
            # cap (MAX_ATTEMPTS) applies: it retries once, then terminalizes as
            # 'failed' instead of requeuing forever. That 'failed' status is what
            # the pull agent relays to the cloud, so the dashboard shows an error
            # instead of an eternal "processing".
            finish(led, key, "fail",
                   "el proceso termino sin escribir un resultado (posible timeout o caida)",
                   rec.get("job_log"), ledger_path, persist=False)
            changed = True
    if changed:
        save_ledger(led, ledger_path)
    return running


def apply_pending_cancels(led, work_dir: Path, ledger_path: Path) -> None:
    """Honor a cancel marker that lands on a job before it started running,
    and reap stale markers left behind by a job that already finished.

    A cancel can race launch: the cloud/agent may drop the marker while the
    record is still 'pending'. The running-jobs poll only sees jobs already in
    the `running` dict, so a pending record needs its own sweep, called right
    after scan_and_enqueue in the main loop.

    This sweeps the whole cancel dir (not just keys already in the ledger) so
    a marker whose job finished (done/failed/canceled) or whose record no
    longer exists gets removed. Left alone, a stale marker would silently
    insta-cancel a FUTURE retry/edit of the same job id and delete its fresh
    input. This reaper cannot lose a genuine cancel: the agent re-writes the
    marker every poll tick while the cloud job still has cancel_requested=1,
    so a fresh task/mp4 for the same id will meet a re-written marker once it
    turns pending/running again.
    """
    dirty = False
    cancel_dir = Path(work_dir) / "cancel"
    for marker in cancel_dir.glob("*.cancel"):
        key = "bionico/" + marker.stem
        rec = led.get(key)
        status = rec.get("status") if rec else None
        if status == "pending":
            rec["status"] = "canceled"
            rec["finished"] = datetime.now().isoformat(timespec="seconds")
            rec["detail"] = "canceled by user"
            _drop_inbox(rec)
            marker.unlink(missing_ok=True)
            dirty = True
            log("CANCELED %s (pending, never started)" % key)
        elif status == "running":
            # Leave the marker; the running-jobs poll consumes it.
            continue
        else:
            # No record, or a terminal one (done/failed/canceled): the marker
            # is stale and must not be allowed to poison a future retry/edit.
            marker.unlink(missing_ok=True)
            log("REAPED stale cancel marker %s" % key)
    if dirty:
        save_ledger(led, ledger_path)


def run_watcher(cfg, interval=2.0):
    """Loop principal del watcher usando bionico.config.Config."""
    _state["work_dir"] = cfg.work_dir
    inbox = cfg.inbox_bionico_dir
    pipeline_dir = cfg.pipeline_dir
    ledger_path = cfg.work_dir / "ledger.json"
    jobs_log_dir = cfg.work_dir / "jobs"
    cfg.ensure_dirs()

    led = load_ledger(ledger_path)
    running = reconcile(led, cfg.work_dir, ledger_path)
    if running:
        log("adopted %d edit(s) still running from before the restart" % len(running))
    cpu_percent()  # prime sampler
    log("worker started (max_jobs=%d, cpu_ceiling=%.0f%%, min_free_ram=%.1fGB, cores=%s)."
        % (MAX_JOBS, CPU_CEILING_PCT, MIN_FREE_RAM_GB, os.cpu_count()))

    try:
        while True:
            cpu = cpu_percent()
            for key in list(running.keys()):
                info = running[key]
                marker = _cancel_marker(cfg.work_dir, key)
                if marker.exists():
                    _kill_tree(info.get("pid"))
                    del running[key]
                    rec = led[key]
                    rec["status"] = "canceled"
                    rec["finished"] = datetime.now().isoformat(timespec="seconds")
                    rec["detail"] = "canceled by user"
                    _drop_inbox(rec)
                    marker.unlink(missing_ok=True)
                    save_ledger(led, ledger_path)
                    log("CANCELED %s (killed pid %s)" % (key, info.get("pid")))
                    continue
                if info["result"].exists():
                    res = _read_result(info["result"])
                    del running[key]
                    _finish_from_result(led, key, res, info.get("job_log"), ledger_path)
                elif not _pid_alive(info["pid"]):
                    del running[key]
                    finish(led, key, "fail", "process ended without writing a result",
                           info.get("job_log"), ledger_path)
                else:
                    reason = watchdog_reason(info, time.time())
                    if reason:
                        _kill_tree(info.get("pid"))
                        del running[key]
                        # Route through finish() so the retry cap applies and
                        # the pull agent relays the failure to the dashboard.
                        finish(led, key, "fail", "watchdog: " + reason,
                               info.get("job_log"), ledger_path)
                        _notify.notify("Watchdog detuvo un trabajo",
                                       "%s: %s" % (key, reason))
                        log("WATCHDOG killed %s (pid %s): %s"
                            % (key, info.get("pid"), reason))
            scan_and_enqueue(led, inbox, ledger_path)
            apply_pending_cancels(led, cfg.work_dir, ledger_path)
            free = free_ram_gb()
            allow = (not running) or (len(running) < MAX_JOBS
                                      and cpu < CPU_CEILING_PCT and free >= MIN_FREE_RAM_GB)
            if allow:
                key = next_pending(led)
                if key is not None:
                    rec = led[key]
                    if resolve_exe("contenido-bionico") is None:
                        log("BLOCKED %s: contenido-bionico not found (finish the install)." % key)
                    else:
                        rec["status"] = "running"
                        rec["attempts"] += 1
                        rec["started"] = datetime.now().isoformat(timespec="seconds")
                        rec.pop("defer_until", None)
                        rec.pop("quota", None)
                        save_ledger(led, ledger_path)
                        status, payload = launch(key, rec, pipeline_dir,
                                                 jobs_log_dir, cfg.work_dir)
                        if status == "started":
                            rec["pid"] = payload["pid"]
                            rec["job_log"] = str(payload["job_log"])
                            save_ledger(led, ledger_path)
                            running[key] = payload
                            log("LAUNCHED %s  [%d/%d running, cpu=%.0f%%, free=%.1fGB]  pid=%s log=%s"
                                % (key, len(running), MAX_JOBS, cpu, free, payload["pid"],
                                   payload["job_log"].name))
                        elif status == "nocli":
                            rec["status"] = "pending"
                            rec["attempts"] -= 1
                            save_ledger(led, ledger_path)
                            log("BLOCKED %s: %s" % (key, payload))
                        else:
                            finish(led, key, "fail", payload, None, ledger_path)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("worker stopping; %d edit(s) keep running detached." % len(running))


def main():
    from bionico import config
    run_watcher(config.load())


if __name__ == "__main__":
    main()
