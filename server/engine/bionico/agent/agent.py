"""Pull agent: bridges cloud dashboard jobs into the local watcher inbox.

Claim -> download -> drop into data/inbox/bionico/ (the watcher does the real
work) -> read the ledger -> upload the finished video -> complete. Stateless
across restarts: cloud `active_jobs` + the local ledger are the truth.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from bionico.agent import preflight
from bionico.agent.client import CloudClient, CONTENT_TYPES
from bionico.agent.cloudcfg import load_cloud
from bionico.autostart import launcher_path
from bionico.notify import notify
from bionico.orchestrator import _detached_popen
from bionico.watcher.watcher import _cancel_marker, _digest, load_ledger, resolve_exe

DONE_PREFIX = "[pipeline] done: "
HEARTBEAT_SECONDS = 60
POLL_SECONDS = 5
MAX_DELIVERY_FAILURES = 5
# Publish failures mirror the delivery cap: after this many consecutive
# failed publish attempts the job terminalizes as failed in the cloud
# instead of blocking the agent forever.
MAX_PUBLISH_FAILURES = MAX_DELIVERY_FAILURES
METRICS_SECONDS = 300

# Quota-aware claiming: above this five-hour utilization % the agent stops
# claiming new jobs until the window resets (metrics refresh every 5 min).
QUOTA_DEFER_PCT = float(os.environ.get("BIONICO_QUOTA_DEFER_PCT") or 85)
# Operator alert threshold for high quota usage (one alert per reset window).
QUOTA_NOTIFY_PCT = 90.0

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# Markers verified against a real job log (run 13_short). Within one line the
# LAST matching entry wins, so generic markers come first and specific ones
# later; across lines the newest line wins.
STAGE_MARKERS = [
    ("[short] rendering", "render"),
    ("de-silence video", "desilence"),
    ("transcribe raw", "transcribe"),
    ("editor reviewer loop", "edit_cut"),
    ("map cleaned transcript", "apply_cut"),
    ("trim spacings", "apply_cut"),
    ("refine words", "apply_cut"),
    ("make render edl", "apply_cut"),
    ("render source.mp4", "apply_cut"),
    ("derive transcript", "apply_cut"),
    ("[pipeline] short:", "captions"),
    ("short_planner", "plan"),
    ("short_author", "animate"),
    ("short_repair", "animate"),
    ("rendering captions", "captions"),
    ("short_caption", "captions"),
    ("[quote]", "quotes"),
    ("[carousel]", "carousel"),
    ("[pipeline] exported", "finalize"),
    ("[pipeline] caption:", "finalize"),
]


def log(msg: str) -> None:
    print("%s  agent: %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def parse_stage(log_text: str) -> str | None:
    """Newest recognizable stage marker in the log tail (case-insensitive)."""
    stage = None
    for line in log_text.splitlines():
        low = line.lower()
        for marker, name in STAGE_MARKERS:
            if marker in low:
                stage = name
    return stage


# Animate-orchestrator progress marker: '[progress] scene=<n>/<total>'.
_PROGRESS_RE = re.compile(r"\[progress\]\s*scene=(\d+)\s*/\s*(\d+)")


def parse_stage_detail(log_text: str) -> dict | None:
    """Newest '[progress] scene=n/total' in the log tail -> {'scene', 'total'}."""
    match = None
    for m in _PROGRESS_RE.finditer(log_text):
        match = m
    if match is None:
        return None
    return {"scene": int(match.group(1)), "total": int(match.group(2))}


def job_log_tail(rec: dict, size: int = 4096) -> str:
    path = rec.get("job_log")
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def sum_run_tokens(run_id: str | None, pipeline_dir: Path) -> int | None:
    if not run_id:
        return None
    logs = Path(pipeline_dir) / "runs" / str(run_id) / "logs"
    total = 0
    seen = False
    for jl in logs.rglob("*.stream.jsonl"):
        try:
            lines = jl.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("type") == "result":
                u = data.get("usage") or {}
                total += sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens"))
                seen = True
                break
    return total if seen else None


def resolve_active_ai_info() -> dict:
    try:
        from contenido_bionico.shared.config import read_env_file
        env_vars = read_env_file()
    except Exception:
        env_vars = {}
    provider = (env_vars.get("AI_PROVIDER") or os.environ.get("AI_PROVIDER") or "auto").lower()
    model = env_vars.get("AI_MODEL_CHOICE") or os.environ.get("AI_MODEL_CHOICE") or "default"

    freellm_key = env_vars.get("FREE_LLM_API_KEY") or os.environ.get("FREE_LLM_API_KEY")
    openrouter_key = env_vars.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    anthropic_key = env_vars.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

    active_provider = "Claude Suscripción"
    active_model = model if model != "default" else "claude-3-5-sonnet"

    if provider == "freellm" or (provider == "auto" and freellm_key):
        active_provider = "FreeLLMAPI"
        active_model = model if model != "default" else "gpt-4o-mini"
    elif provider == "openrouter" or (provider == "auto" and openrouter_key):
        active_provider = "OpenRouter"
        active_model = model if model != "default" else "anthropic/claude-3.5-sonnet"
    elif provider == "anthropic" or (provider == "auto" and anthropic_key):
        active_provider = "Anthropic API"
        active_model = model if model != "default" else "claude-3-5-sonnet-20241022"

    return {"active_provider": active_provider, "active_model": active_model}


_ACTIVE_AI_RE = re.compile(r"\[pipeline\] active_ai: provider=(.*?)\s+model=(.*)")


def parse_active_ai_from_log(log_text: str) -> dict | None:
    for line in reversed(log_text.splitlines()):
        m = _ACTIVE_AI_RE.search(line)
        if m:
            return {"active_provider": m.group(1).strip(), "active_model": m.group(2).strip()}
    return None


def read_usage_metrics() -> dict | None:
    out = resolve_active_ai_info()
    try:
        if CREDENTIALS_PATH.exists():
            creds = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
            token = (creds.get("claudeAiOauth") or {}).get("accessToken")
            if token:
                req = urllib.request.Request(USAGE_URL, headers={
                    "Authorization": "Bearer %s" % token,
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "bionico-agent/1.0",
                })
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                for key, prefix in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
                    block = data.get(key) or {}
                    out[prefix + "_pct"] = block.get("utilization")
                    out[prefix + "_resets_at"] = block.get("resets_at")
    except Exception:
        pass
    return out


def agent_event(work_dir: Path, event: str, **fields) -> None:
    try:
        p = Path(work_dir) / "agent" / "agent-events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
        rec.update(fields)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def parse_done_path(log_text: str) -> str | None:
    path = None
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith(DONE_PREFIX):
            path = line[len(DONE_PREFIX):].strip()
    return path or None


# No trailing space: a line.strip() drops it, and an empty `changed:` list must
# still match (an explicit no-op change, distinct from the line being absent).
CHANGED_PREFIX = "[pipeline] changed:"
UNSUPPORTED_PREFIX = "[pipeline] unsupported:"


def parse_changed(log_text: str) -> set[str] | None:
    """The deliverable categories a change touched (video|carousel|quotes), from
    the newest `[pipeline] changed:` line. None when absent (a normal produce or
    a legacy edit that publishes its whole folder). May be an EMPTY set (a change
    that produced nothing — e.g. an unsupported request)."""
    changed = None
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith(CHANGED_PREFIX):
            rest = line[len(CHANGED_PREFIX):].strip()
            changed = {c.strip() for c in rest.split(",") if c.strip()}
    return changed


def parse_unsupported(log_text: str) -> str | None:
    note = None
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith(UNSUPPORTED_PREFIX):
            note = line[len(UNSUPPORTED_PREFIX):].strip()
    return note or None


# A changed category -> the deliverable filenames it owns. Used to publish ONLY
# what a change touched, so a music edit re-uploads just the video, a carousel
# edit just the slides, etc. (caption.txt is rebuilt from carousel/quote text).
# V6 multi-format deliverables are NOT editable targets in V6.0, so no change
# ever owns them: a video edit owns final_*.mp4 ONLY (never clip_subtitulado/
# video_*/videocarrusel_*), and the quote/carousel V6 variants stay excluded —
# the forked run carries their copies unchanged.
def _owns(category: str, name: str) -> bool:
    if category == "video":
        return name.startswith("final_") and name.endswith(".mp4")
    if category == "carousel":
        return name.startswith("carrusel_slide") or name in ("carousel.json", "caption.txt")
    if category == "quotes":
        if name.startswith(("quote_foto_", "quote_karaoke_")):
            return False  # V6 quote variants: non-editable, carried by the fork
        return name.startswith("quote_") or name in ("quotes.json", "caption.txt")
    return False


def _delta_filter(files: list, changed: set[str]) -> list:
    return [(p, kind) for (p, kind) in files if any(_owns(c, p.name) for c in changed)]


def failure_tail(job_log: Path | None, detail: str | None, lines: int = 50) -> str:
    parts = []
    if detail:
        parts.append(detail)
    if job_log is not None:
        try:
            text = Path(job_log).read_text(encoding="utf-8", errors="replace")
            parts.append("\n".join(text.splitlines()[-lines:]))
        except OSError:
            pass
    return "\n".join(parts) or "unknown failure"


GENERIC_ERROR_ES = ("El procesamiento falló por un error interno. "
                    "Vuelve a intentarlo; si se repite, contacta al operador.")


def customer_error(detail: str | None, tail: str | None = None) -> str:
    """One short Spanish sentence for the dashboard's error field.

    The raw log tail stays in the LOCAL job log (never customer-facing); this
    maps the common failure classes onto something a creator can act on."""
    text = ("%s\n%s" % (detail or "", tail or "")).lower()
    if "no existe el archivo" in text or "no such file" in text:
        return ("El archivo de entrada ya no está disponible en el equipo. "
                "Toca Reintentar o vuelve a subir el video.")
    if "moov atom" in text or "invalid data found" in text:
        return ("El archivo de video llegó dañado o incompleto. "
                "Exporta el video de nuevo y vuelve a subirlo.")
    if ("watchdog" in text or "timeout" in text or "timed out" in text
            or "sin actividad en el log" in text or "tiempo maximo" in text):
        return "El trabajo tardó demasiado y fue detenido. Vuelve a intentarlo."
    if "usage limit" in text or "limite de uso" in text or "quota" in text:
        return ("Se alcanzó el límite de uso de la IA. "
                "El trabajo se reintentará cuando se libere la cuota.")
    if "sin escribir un resultado" in text:
        return "El proceso se interrumpió inesperadamente. Vuelve a intentarlo."
    if "remotion" in text or "render" in text:
        return "Ocurrió un error al generar el video final. Vuelve a intentarlo."
    return GENERIC_ERROR_ES


def ledger_record(work_dir: Path, job_id: str) -> dict | None:
    led = load_ledger(Path(work_dir) / "ledger.json")
    return led.get("bionico/%s" % job_id)


def local_busy(work_dir: Path) -> bool:
    led = load_ledger(Path(work_dir) / "ledger.json")
    return any(r.get("status") in ("pending", "running") for r in led.values())


def drop_into_inbox(inbox_dir: Path, job_id: str, tmp_mp4: Path, filename: str,
                    anim_quality: str | None = None, features: dict | None = None,
                    anim_count: str | None = None) -> None:
    """Sidecar json first, then atomic mp4 rename (mirrors server.distribute)."""
    dest = Path(inbox_dir)
    dest.mkdir(parents=True, exist_ok=True)
    sidecar = {"format": "short", "id": job_id, "source": "cloud", "filename": filename}
    if anim_quality is not None:
        sidecar["anim_quality"] = anim_quality
    if isinstance(features, dict):
        sidecar["features"] = features
    if anim_count:
        sidecar["anim_count"] = anim_count
    jtmp, jfinal = dest / (job_id + ".json.part"), dest / (job_id + ".json")
    jtmp.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    jtmp.replace(jfinal)
    Path(tmp_mp4).replace(dest / (job_id + ".mp4"))


def drop_task_into_inbox(inbox_dir: Path, job: dict) -> None:
    """Atomic drop of an edit task for the watcher (mirrors drop_into_inbox).

    A versioned edit child carries `parent_run_id` (the run to fork from) and no
    run of its own yet, so the task's `run_id` — the run the watcher bases the
    edit on — is the parent's run, and `parent_run_id` signals the watcher to
    fork it (keeping the parent version intact)."""
    dest = Path(inbox_dir)
    dest.mkdir(parents=True, exist_ok=True)
    parent_run_id = job.get("parent_run_id")
    task = {"kind": "edit", "id": job["id"], "source": "cloud",
            "target": job.get("target"), "run_id": parent_run_id or job.get("run_id"),
            "parent_run_id": parent_run_id,
            "instructions": job.get("instructions"),
            "nonce": int(time.time() * 1000)}
    if job.get("target") == "video":
        if job.get("anim_quality") is not None:
            task["anim_quality"] = job.get("anim_quality")
        if job.get("features") is not None:
            task["features"] = job.get("features")
        if job.get("anim_count") is not None:
            task["anim_count"] = job.get("anim_count")
    tmp, final = dest / (job["id"] + ".task.json.part"), dest / (job["id"] + ".task.json")
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(final)


def run_id_from_folder(folder: Path) -> str | None:
    m = re.fullmatch(r"run_(\d+)", Path(folder).name)
    return ("%s_short" % m.group(1)) if m else None


def _numeric_name_key(p: Path) -> tuple[int, str]:
    m = re.search(r"(\d+)", p.stem)
    return (int(m.group(1)) if m else 0, p.name)


# Ordered (glob pattern, kind) rules for collect_deliverables. The four
# shipping V5-2 rules stay FIRST in their exact order (video first: the
# worker's complete() takes the first kind=video entry as the job's
# output_key, which must remain final_*.mp4), followed by the V6
# multi-format rules. A filename is collected once — first pattern wins —
# so broad late patterns (*.txt) can never re-classify caption.txt.
_DELIVERABLE_RULES = (
    ("final_*.mp4", "video"),
    ("quote_*.mp4", "quote"),
    ("carrusel_slide*.png", "slide"),
    ("caption.txt", "caption"),
    ("carousel.json", "meta"),
    ("quotes.json", "meta"),
    # -- V6 multi-format deliverables (additive; non-editable in V6.0) --
    ("clip_subtitulado.mp4", "video"),
    ("video_*.mp4", "video"),
    ("videocarrusel_*.mp4", "video"),
    ("poster_video_*.mp4", "video"),
    ("quote_foto_*.mp4", "quote"),
    ("quote_karaoke_*.mp4", "quote"),
    ("carrusel_*_slide*.png", "slide"),
    ("cita_*.png", "image"),
    ("citas_3en1.png", "image"),
    ("infografia_*.png", "image"),
    ("frase_*.png", "image"),
    ("poster_*.png", "image"),
    ("*.txt", "caption"),
    ("subtitulos.srt", "caption"),
    ("carrusel_linkedin.pdf", "pdf"),
    ("formats_manifest.json", "meta"),
    # No pack_*.zip / entrega.zip: the cloud Worker zips groups ON DEMAND at
    # download, so we upload only the individual files (never the same bytes twice).
)


def collect_deliverables(folder: Path) -> list[tuple[Path, str]]:
    """Every deliverable in a short run's output folder, video first.

    Ordered glob rules with per-filename dedupe (first pattern wins): e.g.
    quote_foto_1.mp4 already matched by quote_*.mp4 is not re-added by its
    own rule. entrega.zip matches no rule, so a leftover master zip from a
    previous publish is never re-collected."""
    folder = Path(folder)
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for pattern, kind in _DELIVERABLE_RULES:
        for p in sorted(folder.glob(pattern), key=_numeric_name_key):
            if p.name in seen or not p.is_file():
                continue
            seen.add(p.name)
            out.append((p, kind))
    return out


def _preflight(client: CloudClient, job: dict, tmp: Path) -> Path:
    """Probe + validate + normalize a downloaded input to .mp4.

    Raises preflight.PreflightRejected (short Spanish reason) BEFORE the
    pipeline ever starts; retries the download once on a corrupt file."""
    return preflight.prepare_input(
        tmp, redownload=lambda: client.download(job["input_url"], tmp))


def _deliver(client: CloudClient, cfg, job: dict) -> None:
    jid = job["id"]
    if job.get("kind") == "edit":
        drop_task_into_inbox(cfg.inbox_bionico_dir, job)
    else:
        filename = job.get("filename") or "video.mp4"
        ext = Path(filename).suffix.lower()
        if ext not in preflight.ACCEPTED_EXTS:
            ext = ".mp4"
        tmp = Path(cfg.work_dir) / "cloud_tmp" / (jid + ext)
        client.download(job["input_url"], tmp)
        tmp = _preflight(client, job, tmp)  # always hands back an .mp4
        drop_into_inbox(cfg.inbox_bionico_dir, jid, tmp, filename,
                        anim_quality=job.get("anim_quality"), features=job.get("features"),
                        anim_count=job.get("anim_count"))
    client.set_status(jid, "processing")
    log("delivered %s to inbox" % jid)
    agent_event(cfg.work_dir, "deliver", job_id=jid, kind=job.get("kind"))


def _try_deliver(client: CloudClient, cfg, job: dict, delivery_failures: dict[str, int]) -> None:
    """Wrap _deliver so a persistently broken delivery terminalizes instead of
    retrying forever every 5s and blocking all claiming."""
    jid = job["id"]
    try:
        _deliver(client, cfg, job)
    except preflight.PreflightRejected as exc:
        # Deterministic input error: retrying can only fail the same way,
        # so terminalize immediately with the customer-facing reason.
        client.set_status(jid, "failed", error=exc.reason)
        delivery_failures[jid] = 0
        log("REJECTED %s at preflight: %s" % (jid, exc.reason))
        agent_event(cfg.work_dir, "preflight_reject", job_id=jid, reason=exc.reason)
        notify("Video rechazado en preflight", "%s: %s" % (jid, exc.reason))
    except Exception as exc:
        count = delivery_failures.get(jid, 0) + 1
        delivery_failures[jid] = count
        log("delivery failed for %s (%d/%d): %s" % (jid, count, MAX_DELIVERY_FAILURES, exc))
        if count >= MAX_DELIVERY_FAILURES:
            client.set_status(jid, "failed",
                              error="No pudimos descargar tu video del servidor. "
                                    "Vuelve a subirlo e intenta de nuevo.")
            delivery_failures[jid] = 0
    else:
        delivery_failures[jid] = 0


def _report_rejected(client: CloudClient, jid: str, reason: str) -> None:
    """Terminal, non-red 'rejected' status; falls back to 'failed' for a
    Worker that predates the rejected status (would answer 400)."""
    try:
        client.set_status(jid, "rejected", error=reason)
    except Exception:
        client.set_status(jid, "failed", error=reason)


def _cleanup_inbox_inputs(cfg, jid: str) -> None:
    """Remove this job's inbox input files. Called only AFTER a successful
    publish (or a terminal rejection): the input must survive until then so a
    failed publish / dashboard Retry never hits a missing file."""
    for suffix in (".mp4", ".json", ".task.json"):
        try:
            (Path(cfg.inbox_bionico_dir) / (jid + suffix)).unlink()
        except OSError:
            pass


_BUNDLE_MAX_FILE_BYTES = 5 * 1024 * 1024
_BUNDLE_MAX_FILES = 400


def build_edit_bundle(run_dir: Path, dest: Path) -> Path | None:
    """Zip the run's small edit substrate (transcript/EDL/manifest json +
    scene .tsx copies -- never videos) as a remote backup so a lost disk does
    not orphan the published versions. Best-effort: None when nothing to pack."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return None
    picked: list[Path] = []
    seen: set[Path] = set()
    for pattern in ("*.json", "*manifest*.json", "*.tsx"):
        for p in sorted(run_dir.rglob(pattern)):
            if p in seen or not p.is_file():
                continue
            if "node_modules" in p.parts or p.suffix.lower() not in (".json", ".tsx"):
                continue
            try:
                if p.stat().st_size > _BUNDLE_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            seen.add(p)
            picked.append(p)
            if len(picked) >= _BUNDLE_MAX_FILES:
                break
        if len(picked) >= _BUNDLE_MAX_FILES:
            break
    if not picked:
        return None
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in picked:
                zf.write(p, arcname=p.relative_to(run_dir).as_posix())
        return Path(dest)
    except Exception as exc:
        log("edit bundle build failed for %s: %s" % (run_dir, exc))
        try:
            Path(dest).unlink()
        except OSError:
            pass
        return None


def _upload_edit_bundle(client: CloudClient, cfg, jid: str, run_id: str | None,
                        folder: Path) -> None:
    """Best-effort remote backup of the edit substrate (interface: a
    distinctive *_bundle.zip in the job's outbox, NOT a customer output)."""
    if not run_id:
        return
    try:
        bundle = build_edit_bundle(Path(cfg.pipeline_dir) / "runs" / run_id,
                                   Path(folder) / ("%s_bundle.zip" % run_id))
        if bundle is None:
            return
        info = client.upload_url(jid, bundle.name)
        client.upload_file(info["url"], bundle, content_type="application/zip")
        agent_event(cfg.work_dir, "bundle_upload", job_id=jid, name=bundle.name)
    except Exception as exc:
        log("edit bundle upload failed for %s: %s" % (jid, exc))


def _publish(client: CloudClient, cfg, job: dict, rec: dict) -> None:
    jid = job["id"]
    text = ""
    if rec.get("job_log"):
        try:
            text = Path(rec["job_log"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    done = parse_done_path(text)
    if not done or not Path(done).exists():
        client.set_status(jid, "failed",
                          error="El proceso terminó pero no se encontró el resultado. "
                                "Vuelve a intentarlo.")
        log("FAILED %s: no output path in job log" % jid)
        return
    p = Path(done)
    folder = p if p.is_dir() else p.parent
    files = collect_deliverables(folder)
    if not files and p.is_file():
        files = [(p, "video")]
    # Versioned change: the pipeline names which deliverable(s) it touched, so we
    # publish ONLY those (a music edit -> just the video, a carousel edit -> just
    # the slides). Absent line = a normal produce/legacy edit -> publish all.
    changed = parse_changed(text)
    if changed is not None:
        if not changed:
            reason = parse_unsupported(text) or "el cambio solicitado no produjo modificaciones"
            _report_rejected(client, jid, reason)
            _cleanup_inbox_inputs(cfg, jid)
            log("CHANGE rejected %s: %s" % (jid, reason))
            agent_event(cfg.work_dir, "change_noop", job_id=jid)
            return
        files = _delta_filter(files, changed)
    if not files:
        client.set_status(jid, "failed",
                          error="El proceso terminó pero no generó entregables. "
                                "Vuelve a intentarlo.")
        log("FAILED %s: no deliverables in %s" % (jid, folder))
        return
    client.set_status(jid, "publishing")
    outputs = []
    for path, kind in files:
        info = client.upload_url(jid, path.name)
        client.upload_file(info["url"], path,
                           content_type=CONTENT_TYPES.get(path.suffix.lower()))
        outputs.append({"name": path.name, "key": info["key"], "kind": kind,
                        "size": path.stat().st_size})
    run_id = run_id_from_folder(folder)
    _upload_edit_bundle(client, cfg, jid, run_id, folder)
    tokens = sum_run_tokens(run_id, cfg.pipeline_dir)
    client.complete(jid, {"run_id": run_id, "outputs": outputs, "tokens_total": tokens})
    _cleanup_inbox_inputs(cfg, jid)  # published: the inbox input is finally disposable
    log("published %s (%d file(s) from %s)" % (jid, len(outputs), folder))
    agent_event(cfg.work_dir, "publish", job_id=jid, files=len(outputs), tokens=tokens)


def _relay_cancel(cfg, jid: str) -> None:
    marker = _cancel_marker(cfg.work_dir, "bionico/%s" % jid)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("cancel", encoding="utf-8")
        log("cancel relayed for %s" % jid)


def drain_reclaims(client: CloudClient, cfg) -> None:
    """Reclaim local disk for versions the dashboard deleted in the cloud.

    The worker queues each deleted version's run id (only the local PC knows
    where its working + output folders live). For each we run the pipeline's
    `reclaim-run`, which drops `runs/<id>/` and `output_short/run_<n>/`, then ack
    so the queue drains. Idempotent: reclaiming an already-gone run is a no-op.

    Best-effort: with no CLI yet (install incomplete) we leave the queue for a
    later tick rather than ack-and-forget; a run whose reclaim subprocess errors
    is skipped (not acked) and retried next tick."""
    run_ids = client.reclaims()
    if not run_ids:
        return
    exe = resolve_exe("contenido-bionico")
    if not exe:
        return  # can't reclaim without the CLI; keep the queue for later
    done = []
    for run_id in run_ids:
        rid = str(run_id)
        try:
            subprocess.run(
                [exe, "reclaim-run", rid], cwd=str(cfg.pipeline_dir), timeout=120,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            done.append(rid)
        except Exception as exc:  # subprocess/timeout: retry next tick
            log("reclaim failed for %s: %s" % (rid, exc))
    if done:
        client.ack_reclaims(done)
        log("reclaimed %d run(s): %s" % (len(done), ", ".join(done)))
        agent_event(cfg.work_dir, "reclaim", run_ids=done)


def _spawn_detached_restart(cfg) -> None:
    """Launch a fully detached `bionico restart --detach` that survives this
    agent process being killed a few seconds later (the restart it triggers
    kills the whole stack, agent included). Reuses the same launcher
    resolution and detached-Popen flags as `bionico start --detach`."""
    cmd = [launcher_path(), "restart", "--detach"]
    _detached_popen(cmd, cfg)


def check_and_apply_restart(client: CloudClient, cfg) -> None:
    """Poll for a dashboard-requested restart and apply it.

    Ack BEFORE spawning the restart, never after: the detached restart kills
    this agent within seconds, and if we hadn't already acked, the
    freshly-restarted agent would see the request still pending and restart
    again -- an infinite loop. Acking first guarantees the post-restart agent
    sees done == requested (not pending), breaking the loop. If the ack
    fails, do nothing and retry on the next poll."""
    try:
        ctrl = client.get_restart_control()
    except Exception:
        return
    if not ctrl or not ctrl.get("restart_pending"):
        return
    try:
        client.ack_restart()
    except Exception:
        return
    log("RESTART requested from dashboard — cycling the stack")
    try:
        agent_event(cfg.work_dir, "server_restart")
    except Exception:
        pass
    try:
        _spawn_detached_restart(cfg)
    except Exception as exc:
        log("restart spawn failed: %s" % exc)


def _stale_upload_drop(cfg, jid: str, rec: dict) -> bool:
    """True when a fresh .mp4 re-drop for this job sits in the inbox but the
    watcher has not ingested it yet (its stat differs from the ledger's).

    Mirrors the edit digest guard: acting on the stale terminal record would
    make a dashboard Retry insta-fail with the previous run's error. A cheap
    size+mtime comparison (the watcher's own dedup key) avoids re-hashing a
    multi-hundred-MB video every 5 s."""
    if rec.get("status") not in ("done", "failed", "canceled"):
        return False
    mp4 = Path(cfg.inbox_bionico_dir) / (jid + ".mp4")
    try:
        st = mp4.stat()
    except OSError:
        return False
    prev_size, prev_mtime = rec.get("size"), rec.get("mtime")
    if prev_size is None or prev_mtime is None:
        return False
    return (int(prev_size) != st.st_size
            or abs(float(prev_mtime) - st.st_mtime) >= 1e-6)


def _heartbeat_wait(client: CloudClient, jid: str, beats: dict[str, float],
                    now: float) -> None:
    if now - beats.get(jid, 0.0) >= HEARTBEAT_SECONDS:
        client.set_status(jid, "processing")
        beats[jid] = now


def _quota_claim_deferred(usage: dict | None) -> bool:
    """True when the five-hour utilization is above the defer threshold, so
    claiming a new job would immediately run into the Claude usage limit."""
    if not usage:
        return False
    # A cached reading whose five-hour window already reset is stale: the
    # quota is back, so resume claiming even if fresh metrics reads keep
    # failing (otherwise a high stale pct would pause claiming forever).
    resets_at = usage.get("five_hour_resets_at")
    if resets_at:
        try:
            reset_ts = datetime.fromisoformat(
                str(resets_at).replace("Z", "+00:00")).timestamp()
            if time.time() > reset_ts:
                return False
        except ValueError:
            pass
    try:
        pct = float(usage.get("five_hour_pct"))
    except (TypeError, ValueError):
        return False
    return pct >= QUOTA_DEFER_PCT


def _tick_job(client: CloudClient, cfg, beats: dict[str, float], now: float,
              delivery_failures: dict[str, int], stages: dict,
              publish_failures: dict[str, int], job: dict) -> None:
    jid = job["id"]
    if job.get("cancel_requested"):
        _relay_cancel(cfg, jid)
    rec = ledger_record(cfg.work_dir, jid)
    if rec is None:
        if job.get("cancel_requested"):
            client.set_status(jid, "canceled")
            log("CANCELED %s (reported)" % jid)
            agent_event(cfg.work_dir, "cancel_report", job_id=jid)
            for suffix in (".task.json", ".mp4"):
                try:
                    (Path(cfg.inbox_bionico_dir) / (jid + suffix)).unlink()
                except OSError:
                    pass
            return
        name = jid + (".task.json" if job.get("kind") == "edit" else ".mp4")
        if not (Path(cfg.inbox_bionico_dir) / name).exists():
            _try_deliver(client, cfg, job, delivery_failures)  # lost between claim and drop: redo
        return
    if job.get("kind") == "edit":
        tf = Path(cfg.inbox_bionico_dir) / (jid + ".task.json")
        if tf.exists() and _digest(tf) != rec.get("digest"):
            # The watcher has not ingested this edit request yet — the
            # ledger record belongs to a previous run of this job id.
            # Heartbeat and wait instead of acting on stale state.
            _heartbeat_wait(client, jid, beats, now)
            return
    elif _stale_upload_drop(cfg, jid, rec):
        # Same guard for uploads: a Retry re-drop the watcher hasn't
        # ingested yet must not surface the previous run's terminal state.
        _heartbeat_wait(client, jid, beats, now)
        return
    status = rec.get("status")
    if status == "done":
        try:
            _publish(client, cfg, job, rec)
        except Exception as exc:
            count = publish_failures.get(jid, 0) + 1
            publish_failures[jid] = count
            log("publish failed for %s (%d/%d): %s"
                % (jid, count, MAX_PUBLISH_FAILURES, exc))
            if count == 3:
                notify("Fallos al publicar",
                       "El trabajo %s lleva %d intentos de publicación fallidos: %s"
                       % (jid, count, exc))
            if count >= MAX_PUBLISH_FAILURES:
                client.set_status(jid, "failed",
                                  error="No se pudo subir el resultado tras varios "
                                        "intentos. Contacta al operador.")
                publish_failures[jid] = 0
        else:
            publish_failures.pop(jid, None)
    elif status == "failed":
        tail = failure_tail(rec.get("job_log"), rec.get("detail"))
        # Customer-facing: one short Spanish sentence. The raw tail stays in
        # the LOCAL job log / agent events, never in the dashboard error.
        client.set_status(jid, "failed", error=customer_error(rec.get("detail"), tail))
        log("FAILED %s (reported to cloud): %s" % (jid, (rec.get("detail") or "")[:200]))
        agent_event(cfg.work_dir, "fail", job_id=jid,
                    detail=(rec.get("detail") or "")[:500])
    elif status == "canceled":
        client.set_status(jid, "canceled")
        log("CANCELED %s (reported)" % jid)
        agent_event(cfg.work_dir, "cancel_report", job_id=jid)
    else:  # pending / running -> heartbeat
        tail = job_log_tail(rec)
        stage = parse_stage(tail)
        detail = parse_stage_detail(tail) or {}
        ai_info = parse_active_ai_from_log(tail) or resolve_active_ai_info()
        detail["provider"] = ai_info.get("active_provider")
        detail["model"] = ai_info.get("active_model")
        if float(rec.get("defer_until") or 0) > now:
            stage = "quota"  # waiting for the Claude usage window to reset
        # Default (None, None): an unknown job with no stage yet must NOT count
        # as a change, or every tick would heartbeat before a stage appears.
        if (stages.get(jid, (None, None)) != (stage, detail)
                or now - beats.get(jid, 0.0) >= HEARTBEAT_SECONDS):
            client.set_status(jid, "processing", stage=stage, stage_detail=detail)
            beats[jid] = now
            stages[jid] = (stage, detail)


def tick(client: CloudClient, cfg, beats: dict[str, float], now: float,
         delivery_failures: dict[str, int], stages: dict | None = None,
         publish_failures: dict[str, int] | None = None,
         usage: dict | None = None) -> None:
    if stages is None:
        stages = {}
    if publish_failures is None:
        publish_failures = {}
    active = client.active_jobs()
    for job in active:
        try:
            _tick_job(client, cfg, beats, now, delivery_failures, stages,
                      publish_failures, job)
        except Exception as exc:
            # One poisoned job must never stop the other jobs' heartbeats or
            # block claiming; it retries on the next tick.
            log("job %s tick error: %s" % (job.get("id"), exc))
    if not active and not local_busy(cfg.work_dir):
        if _quota_claim_deferred(usage):
            return  # defer claiming until the five-hour window resets
        job = client.claim()
        if job is not None:
            agent_event(cfg.work_dir, "claim", job_id=job.get("id"), kind=job.get("kind"))
            _try_deliver(client, cfg, job, delivery_failures)


def main() -> None:
    from bionico import config
    cfg = config.load()
    cloud = load_cloud(cfg.repo_root)
    if cloud is None:
        log("no server/cloud/.env; agent exiting (nothing to do)")
        sys.exit(0)
    client = CloudClient(cloud)
    log("started against %s" % cloud.base_url)
    beats: dict[str, float] = {}
    stages: dict = {}
    delivery_failures: dict[str, int] = {}
    publish_failures: dict[str, int] = {}
    usage: dict | None = None
    quota_notified_reset: str | None = None
    last_metrics = 0.0
    while True:
        try:
            # Metrics are best-effort in their own try/except: a persistently
            # failing metrics POST must never starve the tick below
            # (heartbeats, publishes, claims). last_metrics advances up front
            # so a failure waits the full interval instead of retrying every
            # poll.
            if time.time() - last_metrics >= METRICS_SECONDS:
                last_metrics = time.time()
                m = read_usage_metrics()
                if m:
                    usage = m  # cache BEFORE posting: quota deferral stays fresh
                    client.post_metrics(m)
                    pct = m.get("five_hour_pct")
                    if isinstance(pct, (int, float)) and pct >= QUOTA_NOTIFY_PCT:
                        reset_key = str(m.get("five_hour_resets_at") or "unknown")
                        if reset_key != quota_notified_reset:
                            quota_notified_reset = reset_key
                            notify("Cuota de Claude alta",
                                   "Uso de la ventana de 5 horas al %d%% "
                                   "(se libera: %s)." % (pct, reset_key))
        except Exception as exc:  # metrics failure must never stop the tick
            log("metrics error: %s" % exc)
        try:
            tick(client, cfg, beats, time.time(), delivery_failures, stages,
                 publish_failures, usage)
            drain_reclaims(client, cfg)  # best-effort local disk reclaim on delete
            check_and_apply_restart(client, cfg)  # dashboard-requested restart, ack-before-spawn
        except Exception as exc:  # network blips etc. must never kill the loop
            log("tick error: %s" % exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
