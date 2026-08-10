import json
from pathlib import Path

from bionico.agent import agent


class FakeClient:
    def __init__(self):
        self.calls = []
        self.uploaded = []

    def set_status(self, jid, status, error=None, stage=None, stage_detail=None):
        self.calls.append(("status", jid, status, error, stage))

    def upload_url(self, jid, name):
        self.calls.append(("upload_url", jid, name))
        return {"key": "outbox/%s/%s" % (jid, name), "url": "https://r2/" + name}

    def upload_file(self, url, src, content_type=None):
        self.uploaded.append((url, Path(src).name, content_type))

    def complete(self, jid, payload=None):
        self.calls.append(("complete", jid, payload))


class Cfg:
    def __init__(self, tmp):
        self.work_dir = tmp / "work"
        self.inbox_bionico_dir = tmp / "inbox"
        self.pipeline_dir = tmp / "pipeline"


def test_drop_task_into_inbox_writes_task_file(tmp_path):
    job = {"id": "j1", "kind": "edit", "target": "carousel",
           "run_id": "7_short", "instructions": "make it blue"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert data["kind"] == "edit"
    assert data["target"] == "carousel"
    assert data["run_id"] == "7_short"
    assert data["instructions"] == "make it blue"
    assert isinstance(data["nonce"], int)
    assert not (tmp_path / "inbox" / "j1.task.json.part").exists()
    assert "anim_quality" not in data


def test_drop_task_into_inbox_video_target_includes_anim_quality(tmp_path):
    job = {"id": "j1", "kind": "edit", "target": "video",
           "run_id": "7_short", "instructions": "x", "anim_quality": "high"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert data["anim_quality"] == "high"


def test_drop_task_into_inbox_non_video_target_omits_anim_quality(tmp_path):
    job = {"id": "j1", "kind": "edit", "target": "carousel",
           "run_id": "7_short", "instructions": "x", "anim_quality": "high"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert "anim_quality" not in data


def test_drop_task_into_inbox_video_target_includes_features_and_anim_count(tmp_path):
    job = {"id": "j1", "kind": "edit", "target": "video",
           "run_id": "7_short", "instructions": "x",
           "features": {"camera": False, "music": True}, "anim_count": "few"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert data["features"] == {"camera": False, "music": True}
    assert data["anim_count"] == "few"


def test_drop_task_into_inbox_non_video_target_omits_features_and_anim_count(tmp_path):
    job = {"id": "j1", "kind": "edit", "target": "carousel",
           "run_id": "7_short", "instructions": "x",
           "features": {"camera": False}, "anim_count": "few"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert "features" not in data
    assert "anim_count" not in data


def test_drop_task_into_inbox_carries_parent_run_id(tmp_path):
    # A versioned edit child has no run of its own; the task bases the edit on the
    # parent's run and flags the fork via parent_run_id.
    job = {"id": "j1", "kind": "edit", "target": "video",
           "parent_run_id": "7_short", "instructions": "x"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert data["run_id"] == "7_short"
    assert data["parent_run_id"] == "7_short"


def test_drop_task_into_inbox_without_parent_uses_run_id(tmp_path):
    job = {"id": "j1", "kind": "edit", "target": "carousel",
           "run_id": "5_short", "instructions": "x"}
    agent.drop_task_into_inbox(tmp_path / "inbox", job)
    data = json.loads((tmp_path / "inbox" / "j1.task.json").read_text(encoding="utf-8"))
    assert data["run_id"] == "5_short"
    assert data["parent_run_id"] is None


class ReclaimClient(FakeClient):
    def __init__(self, run_ids):
        super().__init__()
        self._run_ids = run_ids
        self.acked = None

    def reclaims(self):
        return self._run_ids

    def ack_reclaims(self, run_ids):
        self.acked = list(run_ids)


def test_drain_reclaims_runs_cli_and_acks(tmp_path, monkeypatch):
    cfg = Cfg(tmp_path)
    calls = []
    monkeypatch.setattr(agent, "resolve_exe", lambda name: "cbx")
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    client = ReclaimClient(["7_short", "8_short"])
    agent.drain_reclaims(client, cfg)
    assert calls == [["cbx", "reclaim-run", "7_short"], ["cbx", "reclaim-run", "8_short"]]
    assert client.acked == ["7_short", "8_short"]


def test_drain_reclaims_without_cli_leaves_queue(tmp_path, monkeypatch):
    cfg = Cfg(tmp_path)
    monkeypatch.setattr(agent, "resolve_exe", lambda name: None)
    ran = []
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    client = ReclaimClient(["7_short"])
    agent.drain_reclaims(client, cfg)
    assert ran == []             # never shelled out without a CLI
    assert client.acked is None  # queue kept for a later tick


def test_drain_reclaims_empty_is_noop(tmp_path, monkeypatch):
    cfg = Cfg(tmp_path)
    called = []
    monkeypatch.setattr(agent, "resolve_exe", lambda name: called.append("resolve") or "cbx")
    client = ReclaimClient([])
    agent.drain_reclaims(client, cfg)
    assert called == []          # empty queue returns before resolving the CLI
    assert client.acked is None


def test_deliver_edit_drops_task_and_sets_processing(tmp_path):
    client, cfg = FakeClient(), Cfg(tmp_path)
    job = {"id": "j1", "kind": "edit", "target": "quotes",
           "run_id": "7_short", "instructions": "x"}
    agent._deliver(client, cfg, job)
    assert (cfg.inbox_bionico_dir / "j1.task.json").exists()
    assert ("status", "j1", "processing", None, None) in client.calls


def _run_folder(tmp_path):
    folder = tmp_path / "output" / "run_7"
    folder.mkdir(parents=True)
    for name in ("final_7.mp4", "quote_1.mp4", "quote_2.mp4", "quote_10.mp4",
                 "carrusel_slide1.png", "caption.txt", "carousel.json", "quotes.json"):
        (folder / name).write_bytes(b"x")
    return folder


def test_run_id_and_deliverables(tmp_path):
    folder = _run_folder(tmp_path)
    assert agent.run_id_from_folder(folder) == "7_short"
    files = agent.collect_deliverables(folder)
    names = [p.name for p, _ in files]
    kinds = [k for _, k in files]
    assert names[0] == "final_7.mp4" and kinds[0] == "video"
    assert ("quote_1.mp4" in names) and ("carrusel_slide1.png" in names)
    assert ("caption.txt" in names) and ("carousel.json" in names)
    quote_names = [n for n in names if n.startswith("quote_")]
    assert quote_names == ["quote_1.mp4", "quote_2.mp4", "quote_10.mp4"]
    assert kinds.count("quote") == 3 and kinds.count("slide") == 1
    assert kinds.count("caption") == 1 and kinds.count("meta") == 2


def test_publish_uploads_all_and_completes_with_manifest(tmp_path):
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % (folder / "final_7.mp4"), encoding="utf-8")
    client, cfg = FakeClient(), Cfg(tmp_path)
    agent._publish(client, cfg, {"id": "j1"}, {"job_log": str(log)})
    complete = [c for c in client.calls if c[0] == "complete"][0]
    payload = complete[2]
    assert payload["run_id"] == "7_short"
    # The 8 individual deliverables — no entrega.zip (the Worker zips on demand).
    assert len(payload["outputs"]) == 8
    assert payload["outputs"][0]["kind"] == "video"
    assert not any(o["name"] == "entrega.zip" for o in payload["outputs"])
    assert len(client.uploaded) == 8
    for out in payload["outputs"]:
        assert out["size"] == 1  # every fixture file in _run_folder is 1 byte (b"x")
    assert payload["tokens_total"] is None  # no runs/7_short/logs tree under cfg.pipeline_dir


def test_publish_includes_tokens_total_from_run_logs(tmp_path):
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % (folder / "final_7.mp4"), encoding="utf-8")
    client, cfg = FakeClient(), Cfg(tmp_path)
    logs = cfg.pipeline_dir / "runs" / "7_short" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.stream.jsonl").write_text(
        json.dumps({"type": "result", "usage": {
            "input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}),
        encoding="utf-8")
    agent._publish(client, cfg, {"id": "j1"}, {"job_log": str(log)})
    complete = [c for c in client.calls if c[0] == "complete"][0]
    assert complete[2]["tokens_total"] == 30


def test_publish_done_line_pointing_at_folder(tmp_path):
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % folder, encoding="utf-8")
    client, cfg = FakeClient(), Cfg(tmp_path)
    agent._publish(client, cfg, {"id": "j1"}, {"job_log": str(log)})
    assert [c for c in client.calls if c[0] == "complete"]


def test_parse_changed_and_unsupported():
    assert agent.parse_changed("[pipeline] changed: video\n[pipeline] done: /x\n") == {"video"}
    assert agent.parse_changed("[pipeline] changed: carousel,quotes\n") == {"carousel", "quotes"}
    assert agent.parse_changed("[pipeline] changed: \n") == set()   # explicit no-op
    assert agent.parse_changed("no such line\n") is None            # legacy/full publish
    assert agent.parse_unsupported("[pipeline] unsupported: no puedo aun\n") == "no puedo aun"
    assert agent.parse_unsupported("nothing\n") is None


def test_owns_delta_mapping():
    assert agent._owns("video", "final_7.mp4")
    assert not agent._owns("video", "quote_1.mp4")
    assert agent._owns("carousel", "carrusel_slide1.png")
    assert agent._owns("carousel", "caption.txt")
    assert agent._owns("quotes", "quote_2.mp4")
    assert agent._owns("quotes", "caption.txt")
    assert not agent._owns("quotes", "final_7.mp4")


def test_publish_delta_uploads_only_changed_deliverable(tmp_path):
    folder = _run_folder(tmp_path)  # video + quotes + slide + caption + metas
    log = tmp_path / "job.log"
    log.write_text("[pipeline] changed: video\n[pipeline] done: %s\n" % folder, encoding="utf-8")
    client, cfg = FakeClient(), Cfg(tmp_path)
    agent._publish(client, cfg, {"id": "j1"}, {"job_log": str(log)})
    complete = [c for c in client.calls if c[0] == "complete"][0]
    names = [o["name"] for o in complete[2]["outputs"]]
    # Only the video, not the carousel/quotes/caption (and no entrega.zip).
    assert names == ["final_7.mp4"]


def test_publish_unsupported_change_rejects_the_version(tmp_path):
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text(
        "[pipeline] changed: \n[pipeline] unsupported: no puedo recortar todavia\n"
        "[pipeline] done: %s\n" % folder, encoding="utf-8")
    client, cfg = FakeClient(), Cfg(tmp_path)
    agent._publish(client, cfg, {"id": "j1"}, {"job_log": str(log)})
    # Interface #5: a change that could not be applied reports 'rejected'
    # (terminal, non-red) with the short Spanish reason.
    rejects = [c for c in client.calls if c[0] == "status" and c[2] == "rejected"]
    assert rejects and "recortar" in (rejects[0][3] or "")
    assert not any(c[0] == "complete" for c in client.calls)  # nothing published


def test_publish_unsupported_change_falls_back_to_failed_on_old_worker(tmp_path):
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text(
        "[pipeline] changed: \n[pipeline] unsupported: no puedo recortar todavia\n"
        "[pipeline] done: %s\n" % folder, encoding="utf-8")

    class OldWorker(FakeClient):
        def set_status(self, jid, status, error=None, stage=None, stage_detail=None):
            if status == "rejected":
                raise IOError("HTTP 400: invalid status")  # pre-rejected Worker
            super().set_status(jid, status, error=error, stage=stage,
                               stage_detail=stage_detail)

    client, cfg = OldWorker(), Cfg(tmp_path)
    agent._publish(client, cfg, {"id": "j1"}, {"job_log": str(log)})
    fails = [c for c in client.calls if c[0] == "status" and c[2] == "failed"]
    assert fails and "recortar" in (fails[0][3] or "")


# --- C1: agent must not act on a stale ledger record for an edit job ---------

class ActiveClient(FakeClient):
    def __init__(self, jobs):
        super().__init__()
        self._jobs = jobs

    def active_jobs(self):
        return self._jobs


def _write_ledger(cfg, jid, rec):
    """Write the real ledger file (watcher.save_ledger writes a plain JSON dict)."""
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.work_dir / "ledger.json", "w", encoding="utf-8") as f:
        json.dump({"bionico/%s" % jid: rec}, f)


def _drop_task_file(cfg, jid, content=b"new-edit-bytes"):
    cfg.inbox_bionico_dir.mkdir(parents=True, exist_ok=True)
    (cfg.inbox_bionico_dir / (jid + ".task.json")).write_bytes(content)


def test_tick_stale_done_record_waits_and_heartbeats(tmp_path):
    # A prior run of this job id left a `done` record (with a real run folder
    # that WOULD publish). A fresh edit request was just dropped with different
    # bytes; the watcher has not ingested it yet (rec digest is stale).
    jid = "j1"
    cfg = Cfg(tmp_path)
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % (folder / "final_7.mp4"), encoding="utf-8")
    _write_ledger(cfg, jid, {"status": "done", "digest": "oldsha", "job_log": str(log)})
    _drop_task_file(cfg, jid)
    client = ActiveClient([{"id": jid, "kind": "edit", "target": "carousel"}])
    agent.tick(client, cfg, beats={}, now=1000.0, delivery_failures={})
    # Must NOT publish the stale outputs.
    assert not any(c[0] in ("complete", "upload_url") for c in client.calls)
    assert not client.uploaded
    # Must heartbeat exactly once for this job and do nothing else.
    assert client.calls == [("status", jid, "processing", None, None)]


def test_tick_stale_failed_record_does_not_report_failed(tmp_path):
    jid = "j1"
    cfg = Cfg(tmp_path)
    _write_ledger(cfg, jid, {"status": "failed", "digest": "oldsha",
                             "detail": "old error"})
    _drop_task_file(cfg, jid)
    client = ActiveClient([{"id": jid, "kind": "edit", "target": "quotes"}])
    agent.tick(client, cfg, beats={}, now=1000.0, delivery_failures={})
    assert not any(c[0] == "status" and c[2] == "failed" for c in client.calls)
    assert client.calls == [("status", jid, "processing", None, None)]


def test_tick_done_record_with_task_absent_publishes(tmp_path):
    # File absent + rec done -> the watcher finished (deleted the task file):
    # publish as before. Digest matching is irrelevant when the file is gone.
    jid = "j1"
    cfg = Cfg(tmp_path)
    folder = _run_folder(tmp_path)
    log = tmp_path / "job.log"
    log.write_text("[pipeline] done: %s\n" % (folder / "final_7.mp4"), encoding="utf-8")
    _write_ledger(cfg, jid, {"status": "done", "digest": "oldsha", "job_log": str(log)})
    cfg.inbox_bionico_dir.mkdir(parents=True, exist_ok=True)  # no .task.json inside
    client = ActiveClient([{"id": jid, "kind": "edit", "target": "carousel"}])
    agent.tick(client, cfg, beats={}, now=1000.0, delivery_failures={})
    assert [c for c in client.calls if c[0] == "complete"]
