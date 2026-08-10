import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bionico.agent import agent as ag
from bionico.agent.client import CloudClient
from bionico.agent.cloudcfg import CloudConfig


def test_parse_stage_cut_then_short_then_author_returns_animate():
    log = "\n".join([
        "[pipeline] cut: video_id=12",
        "editor reviewer loop starting",
        "map cleaned transcript",
        "[pipeline] short: video_id=12",
        "rendering captions.webm via Remotion",
        "short_author writing scene 1",
    ])
    assert ag.parse_stage(log) == "animate"


def test_parse_stage_earlier_only_lines_return_earlier_stage():
    log = "\n".join([
        "de-silence video starting",
        "transcribe raw audio",
    ])
    assert ag.parse_stage(log) == "transcribe"


def test_parse_stage_within_one_line_last_match_wins():
    # Both a generic and a specific marker appear on the SAME line; the later
    # (rightmost) entry in STAGE_MARKERS that matches wins per the module
    # docstring ("within one line the LAST matching entry wins").
    log = "short_planner then short_repair on the same line"
    assert ag.parse_stage(log) == "animate"


def test_parse_stage_no_markers_returns_none():
    assert ag.parse_stage("noise\nmore noise\n") is None


def test_parse_stage_empty_string_returns_none():
    assert ag.parse_stage("") is None


def test_job_log_tail_reads_end_of_real_file(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("x" * 5000 + "TAIL_MARKER_END", encoding="utf-8")
    tail = ag.job_log_tail({"job_log": str(log)}, size=4096)
    assert "TAIL_MARKER_END" in tail
    assert len(tail) <= 4096 + 20  # decoding slack, still bounded


def test_job_log_tail_missing_job_log_key_returns_empty():
    assert ag.job_log_tail({}) == ""


def test_job_log_tail_missing_file_returns_empty(tmp_path):
    assert ag.job_log_tail({"job_log": str(tmp_path / "nope.log")}) == ""


def _write_stream_jsonl(path, usage=None, extra_lines_before=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for _ in range(extra_lines_before):
        lines.append(json.dumps({"type": "assistant", "message": "noise"}))
    if usage is not None:
        lines.append(json.dumps({"type": "result", "usage": usage}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_sum_run_tokens_sums_two_files_all_four_usage_numbers(tmp_path):
    run_id = "12_short"
    logs = tmp_path / "runs" / run_id / "logs"
    _write_stream_jsonl(logs / "a.stream.jsonl", usage={
        "input_tokens": 10, "output_tokens": 20,
        "cache_creation_input_tokens": 5, "cache_read_input_tokens": 1,
    })
    _write_stream_jsonl(logs / "sub" / "b.stream.jsonl", usage={
        "input_tokens": 100, "output_tokens": 200,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3,
    })
    total = ag.sum_run_tokens(run_id, tmp_path)
    assert total == (10 + 20 + 5 + 1) + (100 + 200 + 0 + 3)


def test_sum_run_tokens_uses_last_result_line_per_file(tmp_path):
    run_id = "12_short"
    logs = tmp_path / "runs" / run_id / "logs"
    logs.mkdir(parents=True)
    lines = [
        json.dumps({"type": "result", "usage": {"input_tokens": 999}}),
        json.dumps({"type": "assistant"}),
        json.dumps({"type": "result", "usage": {"input_tokens": 7}}),
    ]
    (logs / "a.stream.jsonl").write_text("\n".join(lines), encoding="utf-8")
    assert ag.sum_run_tokens(run_id, tmp_path) == 7


def test_sum_run_tokens_no_files_returns_none(tmp_path):
    run_id = "12_short"
    (tmp_path / "runs" / run_id / "logs").mkdir(parents=True)
    assert ag.sum_run_tokens(run_id, tmp_path) is None


def test_sum_run_tokens_no_run_id_returns_none(tmp_path):
    assert ag.sum_run_tokens(None, tmp_path) is None


def test_sum_run_tokens_missing_logs_dir_returns_none(tmp_path):
    assert ag.sum_run_tokens("99_short", tmp_path) is None


# --- client.set_status(stage=) / post_metrics ------------------------------

class Stub(BaseHTTPRequestHandler):
    calls: list = []
    responses: dict = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        key = f"{self.command} {self.path}"
        Stub.calls.append({"key": key, "body": body})
        status, payload = Stub.responses.get(key, (200, b"{}"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    Stub.calls, Stub.responses = [], {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_client(base):
    return CloudClient(CloudConfig(base_url=base, agent_token="tok"))


def test_set_status_sends_stage(server):
    Stub.responses["POST /api/agent/jobs/abc/status"] = (200, b"{}")
    make_client(server).set_status("abc", "processing", stage="animate")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"status": "processing", "stage": "animate"}


def test_set_status_omits_stage_when_none(server):
    Stub.responses["POST /api/agent/jobs/abc/status"] = (200, b"{}")
    make_client(server).set_status("abc", "canceled")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"status": "canceled"}


def test_post_metrics_posts_payload(server):
    Stub.responses["POST /api/agent/metrics"] = (200, b"{}")
    make_client(server).post_metrics({"five_hour_pct": 42})
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"five_hour_pct": 42}


# --- read_usage_metrics -----------------------------------------------------

class FakeResp:
    def __init__(self, data):
        self._data = json.dumps(data).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_read_usage_metrics_returns_pct_fields(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tkn"}}), encoding="utf-8")
    monkeypatch.setattr(ag, "CREDENTIALS_PATH", creds)

    captured = {}

    def fake_urlopen(req, timeout=20):
        captured["headers"] = dict(req.header_items())
        return FakeResp({
            "five_hour": {"utilization": 55, "resets_at": "2026-07-04T12:00:00Z"},
            "seven_day": {"utilization": 10, "resets_at": "2026-07-10T00:00:00Z"},
        })

    monkeypatch.setattr(ag.urllib.request, "urlopen", fake_urlopen)
    out = ag.read_usage_metrics()
    assert out == {
        "five_hour_pct": 55, "five_hour_resets_at": "2026-07-04T12:00:00Z",
        "seven_day_pct": 10, "seven_day_resets_at": "2026-07-10T00:00:00Z",
    }
    assert captured["headers"]["Authorization"] == "Bearer tkn"


def test_read_usage_metrics_missing_credentials_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "CREDENTIALS_PATH", tmp_path / "nope.json")
    assert ag.read_usage_metrics() is None


def test_read_usage_metrics_missing_token_returns_none(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {}}), encoding="utf-8")
    monkeypatch.setattr(ag, "CREDENTIALS_PATH", creds)
    assert ag.read_usage_metrics() is None


def test_read_usage_metrics_request_exception_returns_none(tmp_path, monkeypatch):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tkn"}}), encoding="utf-8")
    monkeypatch.setattr(ag, "CREDENTIALS_PATH", creds)

    def raiser(req, timeout=20):
        raise OSError("network down")

    monkeypatch.setattr(ag.urllib.request, "urlopen", raiser)
    assert ag.read_usage_metrics() is None


# --- agent_event jsonl -------------------------------------------------------

def test_agent_event_appends_jsonl_record(tmp_path):
    ag.agent_event(tmp_path, "claim", job_id="j1", kind="video")
    ag.agent_event(tmp_path, "deliver", job_id="j1")
    p = tmp_path / "agent" / "agent-events.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0["event"] == "claim"
    assert rec0["job_id"] == "j1"
    assert rec0["kind"] == "video"
    assert "ts" in rec0
    rec1 = json.loads(lines[1])
    assert rec1["event"] == "deliver"
