import json
import time
from pathlib import Path

from bionico.agent import agent as ag


class FakeClient:
    def __init__(self, active=None, claim_job=None, download_raises=False):
        self._active = active or []
        self._claim = claim_job
        self._download_raises = download_raises
        self.calls = []
        self.downloads = []
        self.uploads = []

    def active_jobs(self):
        return self._active

    def claim(self):
        self.calls.append(("claim",))
        return self._claim

    def set_status(self, job_id, status, error=None, stage=None, stage_detail=None):
        self.calls.append(("status", job_id, status, error, stage))
        self.stage_details = getattr(self, "stage_details", [])
        self.stage_details.append(stage_detail)

    def upload_url(self, job_id, name):
        self.calls.append(("upload_url", job_id, name))
        return {"key": "outbox/%s/%s" % (job_id, name), "url": "http://x/out/" + name}

    def complete(self, job_id, payload=None):
        self.calls.append(("complete", job_id, payload))

    def download(self, url, dest):
        self.downloads.append((url, dest))
        if self._download_raises:
            raise IOError("connection reset")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"VIDEO")

    def upload_file(self, url, src, content_type=None):
        self.uploads.append((url, str(src), content_type))


class FakeCfg:
    def __init__(self, tmp_path):
        self.work_dir = tmp_path / "_work"
        self.inbox_bionico_dir = tmp_path / "inbox" / "bionico"
        self.pipeline_dir = tmp_path / "pipeline"
        self.work_dir.mkdir(parents=True)
        self.inbox_bionico_dir.mkdir(parents=True)


def write_ledger(cfg, records):
    (cfg.work_dir / "ledger.json").write_text(json.dumps(records), encoding="utf-8")


def job(jid="abc123def456", status="claimed"):
    return {"id": jid, "filename": "clip.mp4", "format": "short", "status": status,
            "input_url": "http://x/in", "output_put_url": "http://x/out"}


def test_drop_into_inbox_writes_features_and_anim_count(tmp_path):
    inbox = tmp_path / "inbox"
    src = tmp_path / "src.mp4"
    src.write_bytes(b"VIDEO")
    ag.drop_into_inbox(inbox, "jid1", src, "clip.mp4",
                       features={"camera": False, "music": True}, anim_count="max")
    data = json.loads((inbox / "jid1.json").read_text(encoding="utf-8"))
    assert data["features"] == {"camera": False, "music": True}
    assert data["anim_count"] == "max"


def test_drop_into_inbox_omits_features_and_anim_count_when_absent(tmp_path):
    inbox = tmp_path / "inbox"
    src = tmp_path / "src.mp4"
    src.write_bytes(b"VIDEO")
    ag.drop_into_inbox(inbox, "jid2", src, "clip.mp4")
    data = json.loads((inbox / "jid2.json").read_text(encoding="utf-8"))
    assert "features" not in data
    assert "anim_count" not in data


def test_deliver_new_upload_passes_features_and_anim_count(tmp_path):
    cfg = FakeCfg(tmp_path)
    c = FakeClient()
    j = job()
    j["features"] = {"animations": False}
    j["anim_count"] = "few"
    ag._deliver(c, cfg, j)
    data = json.loads((cfg.inbox_bionico_dir / (j["id"] + ".json")).read_text(encoding="utf-8"))
    assert data["features"] == {"animations": False}
    assert data["anim_count"] == "few"


def test_parse_done_path():
    log = "noise\n[pipeline] cut: video_id=12\n[pipeline] done: C:\\out\\run_12\\final_12.mp4\n"
    assert ag.parse_done_path(log) == "C:\\out\\run_12\\final_12.mp4"
    assert ag.parse_done_path("no done line") is None


def test_done_job_publishes_and_completes(tmp_path):
    cfg = FakeCfg(tmp_path)
    final = tmp_path / "final.mp4"
    final.write_bytes(b"RESULT")
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % final, encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "done", "job_log": str(log)}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert ("status", "abc123def456", "publishing", None, None) in c.calls
    # Only the individual deliverable(s) are uploaded — no entrega.zip (the cloud
    # Worker zips groups on demand at download).
    assert len(c.uploads) == 1
    assert c.uploads[0][1] == str(final)
    complete = [x for x in c.calls if x[0] == "complete"][0]
    assert complete[1] == "abc123def456"
    payload = complete[2]
    assert payload["run_id"] is None
    assert len(payload["outputs"]) == 1
    assert payload["outputs"][0]["kind"] == "video"
    assert payload["outputs"][0]["size"] == len(b"RESULT")
    assert not any(o["name"] == "entrega.zip" for o in payload["outputs"])
    assert payload["tokens_total"] is None


def test_done_without_done_line_reports_failed(tmp_path):
    cfg = FakeCfg(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("it ran but printed nothing useful", encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "done", "job_log": str(log)}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    fails = [x for x in c.calls if x[0] == "status" and x[2] == "failed"]
    assert fails and "no se encontró el resultado" in fails[0][3]
    assert not c.uploads


def test_failed_job_reports_short_spanish_error_not_log_tail(tmp_path):
    cfg = FakeCfg(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("\n".join("line%d" % i for i in range(80)), encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456":
                       {"status": "failed", "job_log": str(log), "detail": "CLI exit code 3"}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    fails = [x for x in c.calls if x[0] == "status" and x[2] == "failed"]
    assert fails
    # Customer-facing: one short Spanish sentence; the raw tail stays local.
    assert fails[0][3] == ag.GENERIC_ERROR_ES
    assert "line79" not in fails[0][3]
    assert "CLI exit code" not in fails[0][3]


def test_failed_watchdog_detail_maps_to_timeout_message(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/abc123def456":
                       {"status": "failed",
                        "detail": "watchdog: sin actividad en el log por 30 minutos"}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    fails = [x for x in c.calls if x[0] == "status" and x[2] == "failed"]
    assert fails and "tardó demasiado" in fails[0][3]


def test_customer_error_classes():
    assert "dañado" in ag.customer_error("CLI exit code 1", "moov atom not found")
    assert "tardó demasiado" in ag.customer_error("watchdog: tiempo maximo", "")
    assert "límite de uso" in ag.customer_error("", "Claude AI usage limit reached|123")
    assert "interrumpió" in ag.customer_error(
        "el proceso termino sin escribir un resultado", "")
    assert ag.customer_error("CLI exit code 9", "???") == ag.GENERIC_ERROR_ES


def test_running_job_heartbeats_at_most_every_60s(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running"}})
    c = FakeClient(active=[job(status="processing")])
    beats = {}
    now = time.time()
    ag.tick(c, cfg, beats, now=now, delivery_failures={})
    ag.tick(c, cfg, beats, now=now + 10, delivery_failures={})   # too soon: no second beat
    ag.tick(c, cfg, beats, now=now + 70, delivery_failures={})   # beat again
    beats_sent = [x for x in c.calls if x[0] == "status" and x[2] == "processing"]
    assert len(beats_sent) == 2


def test_lost_job_is_redelivered(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {})  # no ledger record, no inbox file
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert c.downloads, "input should be re-downloaded"
    assert (cfg.inbox_bionico_dir / "abc123def456.mp4").read_bytes() == b"VIDEO"
    assert json.loads((cfg.inbox_bionico_dir / "abc123def456.json").read_text())["source"] == "cloud"


def test_claims_only_when_idle(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/other": {"status": "running"}})
    c = FakeClient(active=[], claim_job=job())
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert ("claim",) not in c.calls  # local machine busy -> no claim

    write_ledger(cfg, {"bionico/other": {"status": "done"}})
    c2 = FakeClient(active=[], claim_job=job())
    ag.tick(c2, cfg, beats={}, now=time.time(), delivery_failures={})
    assert ("claim",) in c2.calls
    assert (cfg.inbox_bionico_dir / "abc123def456.mp4").exists()
    assert ("status", "abc123def456", "processing", None, None) in c2.calls


def test_persistent_delivery_failure_terminalizes(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {})  # no ledger record, no inbox file
    c = FakeClient(active=[job()], download_raises=True)
    delivery_failures: dict = {}
    for _ in range(5):
        ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures=delivery_failures)
    fails = [x for x in c.calls if x[0] == "status" and x[2] == "failed"]
    assert len(fails) == 1
    assert "No pudimos descargar" in fails[0][3]
    # Earlier ticks (1-4 failures) must not have marked it failed yet.
    assert len([x for x in c.calls if x[0] == "status" and x[2] == "failed"]) == 1


def test_heartbeat_fires_early_when_stage_changes(tmp_path):
    cfg = FakeCfg(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("transcribe raw audio\n", encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running", "job_log": str(log)}})
    c = FakeClient(active=[job(status="processing")])
    beats: dict = {}
    stages: dict = {}
    now = time.time()
    ag.tick(c, cfg, beats, now=now, delivery_failures={}, stages=stages)
    # Well under HEARTBEAT_SECONDS later, but the log now shows a new stage.
    log.write_text("editor reviewer loop\nmap cleaned transcript\n", encoding="utf-8")
    ag.tick(c, cfg, beats, now=now + 5, delivery_failures={}, stages=stages)
    beats_sent = [x for x in c.calls if x[0] == "status" and x[2] == "processing"]
    assert len(beats_sent) == 2
    assert beats_sent[0][4] == "transcribe"
    assert beats_sent[1][4] == "apply_cut"


def test_heartbeat_same_stage_within_window_sends_once(tmp_path):
    cfg = FakeCfg(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("transcribe raw audio\n", encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running", "job_log": str(log)}})
    c = FakeClient(active=[job(status="processing")])
    beats: dict = {}
    stages: dict = {}
    now = time.time()
    ag.tick(c, cfg, beats, now=now, delivery_failures={}, stages=stages)
    ag.tick(c, cfg, beats, now=now + 5, delivery_failures={}, stages=stages)  # same stage, too soon
    beats_sent = [x for x in c.calls if x[0] == "status" and x[2] == "processing"]
    assert len(beats_sent) == 1


def test_cancel_requested_writes_marker_and_relays_once(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running"}})
    j = job(status="processing")
    j["cancel_requested"] = True
    c = FakeClient(active=[j])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    marker = cfg.work_dir / "cancel" / "abc123def456.cancel"
    assert marker.exists()


def test_cancel_requested_rewrites_marker_when_absent_each_tick(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running"}})
    j = job(status="processing")
    j["cancel_requested"] = True
    c = FakeClient(active=[j])
    now = time.time()
    ag.tick(c, cfg, beats={}, now=now, delivery_failures={})
    marker = cfg.work_dir / "cancel" / "abc123def456.cancel"
    assert marker.exists()
    marker.unlink()  # simulate the watcher reaping a stale marker
    ag.tick(c, cfg, beats={}, now=now + 1, delivery_failures={})
    assert marker.exists(), "agent must re-write the marker while cancel_requested holds"


def test_canceled_ledger_status_reports_canceled(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/abc123def456": {"status": "canceled"}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert ("status", "abc123def456", "canceled", None, None) in c.calls


def test_rec_none_with_cancel_requested_reports_canceled_and_skips_redelivery(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {})  # no ledger record
    j = job()
    j["cancel_requested"] = True
    c = FakeClient(active=[j])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert ("status", "abc123def456", "canceled", None, None) in c.calls
    assert not c.downloads, "must not redeliver a job that is already canceled"


def test_rec_none_with_cancel_requested_removes_undelivered_inbox_file(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {})
    stray = cfg.inbox_bionico_dir / "abc123def456.mp4"
    stray.write_bytes(b"partial")
    j = job()
    j["cancel_requested"] = True
    c = FakeClient(active=[j])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert not stray.exists()


# --- stage_detail (interface #4) ---------------------------------------------

def test_parse_stage_detail_last_marker_wins():
    log = "[progress] scene=1/12\nnoise\n[progress] scene=3/12\n"
    assert ag.parse_stage_detail(log) == {"scene": 3, "total": 12}
    assert ag.parse_stage_detail("no markers") is None


def test_heartbeat_includes_stage_detail(tmp_path):
    cfg = FakeCfg(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("short_author writing\n[progress] scene=2/9\n", encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running", "job_log": str(log)}})
    c = FakeClient(active=[job(status="processing")])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert c.stage_details[-1] == {"scene": 2, "total": 9}


def test_heartbeat_fires_early_when_scene_progress_changes(tmp_path):
    cfg = FakeCfg(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("short_author\n[progress] scene=1/9\n", encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "running", "job_log": str(log)}})
    c = FakeClient(active=[job(status="processing")])
    beats, stages = {}, {}
    now = time.time()
    ag.tick(c, cfg, beats, now=now, delivery_failures={}, stages=stages)
    log.write_text("short_author\n[progress] scene=2/9\n", encoding="utf-8")
    ag.tick(c, cfg, beats, now=now + 5, delivery_failures={}, stages=stages)
    beats_sent = [x for x in c.calls if x[0] == "status" and x[2] == "processing"]
    assert len(beats_sent) == 2


# --- quota-aware claiming (interface #8) --------------------------------------

def test_claim_deferred_when_five_hour_usage_high(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {})
    c = FakeClient(active=[], claim_job=job())
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={},
            usage={"five_hour_pct": 92})
    assert ("claim",) not in c.calls

    c2 = FakeClient(active=[], claim_job=job())
    ag.tick(c2, cfg, beats={}, now=time.time(), delivery_failures={},
            usage={"five_hour_pct": 40})
    assert ("claim",) in c2.calls


def test_quota_deferred_job_heartbeats_stage_quota(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/abc123def456":
                       {"status": "pending", "defer_until": time.time() + 3600}})
    c = FakeClient(active=[job(status="processing")])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    beats_sent = [x for x in c.calls if x[0] == "status" and x[2] == "processing"]
    assert beats_sent and beats_sent[0][4] == "quota"


# --- publish resilience --------------------------------------------------------

def test_publish_failure_counter_terminalizes_after_cap(tmp_path, monkeypatch):
    cfg = FakeCfg(tmp_path)
    final = tmp_path / "final.mp4"
    final.write_bytes(b"RESULT")
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % final, encoding="utf-8")
    write_ledger(cfg, {"bionico/abc123def456": {"status": "done", "job_log": str(log)}})

    def boom(client, cfg_, job_, rec_):
        raise IOError("upload reset")

    monkeypatch.setattr(ag, "_publish", boom)
    c = FakeClient(active=[job()])
    publish_failures: dict = {}
    for _ in range(ag.MAX_PUBLISH_FAILURES):
        ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={},
                publish_failures=publish_failures)
    fails = [x for x in c.calls if x[0] == "status" and x[2] == "failed"]
    assert len(fails) == 1
    assert "No se pudo subir" in fails[0][3]


def test_one_poisoned_job_does_not_stop_other_jobs(tmp_path):
    cfg = FakeCfg(tmp_path)
    write_ledger(cfg, {"bionico/badjob00": {"status": "canceled"},
                       "bionico/goodjob0": {"status": "running"}})

    class Picky(FakeClient):
        def set_status(self, job_id, status, error=None, stage=None, stage_detail=None):
            if job_id == "badjob00":
                raise IOError("boom")
            super().set_status(job_id, status, error=error, stage=stage,
                               stage_detail=stage_detail)

    c = Picky(active=[job("badjob00"), job("goodjob0", status="processing")])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    beats_sent = [x for x in c.calls if x[0] == "status" and x[1] == "goodjob0"]
    assert beats_sent, "the healthy job must still heartbeat"


# --- stale upload re-drop guard -------------------------------------------------

def test_stale_failed_record_with_fresh_redrop_waits(tmp_path):
    cfg = FakeCfg(tmp_path)
    mp4 = cfg.inbox_bionico_dir / "abc123def456.mp4"
    mp4.write_bytes(b"FRESH RETRY BYTES")
    st = mp4.stat()
    # Ledger still holds the PREVIOUS run's terminal state and old stat.
    write_ledger(cfg, {"bionico/abc123def456":
                       {"status": "failed", "detail": "old error",
                        "size": st.st_size + 5, "mtime": st.st_mtime - 100}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert not any(x[0] == "status" and x[2] == "failed" for x in c.calls)
    assert ("status", "abc123def456", "processing", None, None) in c.calls


def test_matching_stat_failed_record_still_reports_failed(tmp_path):
    cfg = FakeCfg(tmp_path)
    mp4 = cfg.inbox_bionico_dir / "abc123def456.mp4"
    mp4.write_bytes(b"SAME OLD BYTES")
    st = mp4.stat()
    write_ledger(cfg, {"bionico/abc123def456":
                       {"status": "failed", "detail": "real error",
                        "size": st.st_size, "mtime": st.st_mtime}})
    c = FakeClient(active=[job()])
    ag.tick(c, cfg, beats={}, now=time.time(), delivery_failures={})
    assert any(x[0] == "status" and x[2] == "failed" for x in c.calls)
