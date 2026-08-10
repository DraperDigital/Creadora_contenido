import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bionico.agent.client import CloudClient
from bionico.agent.cloudcfg import CloudConfig


class Stub(BaseHTTPRequestHandler):
    calls: list = []
    responses: dict = {}
    content_length_override: dict = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        key = f"{self.command} {self.path}"
        Stub.calls.append({"key": key, "body": body,
                           "auth": self.headers.get("Authorization"),
                           "length": self.headers.get("Content-Length"),
                           "ua": self.headers.get("User-Agent"),
                           "content_type": self.headers.get("Content-Type")})
        status, payload = Stub.responses.get(key, (404, b"{}"))
        self.send_response(status)
        override = Stub.content_length_override.get(key)
        self.send_header("Content-Length", str(override if override is not None else len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, *a):  # keep test output clean
        pass


@pytest.fixture()
def server():
    Stub.calls, Stub.responses, Stub.content_length_override = [], {}, {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_client(base):
    return CloudClient(CloudConfig(base_url=base, agent_token="tok"))


def test_claim_none_on_204(server):
    Stub.responses["POST /api/agent/claim"] = (204, b"")
    assert make_client(server).claim() is None


def test_claim_returns_job_and_sends_bearer(server):
    job = {"id": "abc", "input_url": "http://x/i", "output_put_url": "http://x/o"}
    Stub.responses["POST /api/agent/claim"] = (200, json.dumps(job).encode())
    got = make_client(server).claim()
    assert got["id"] == "abc"
    assert Stub.calls[0]["auth"] == "Bearer tok"
    assert Stub.calls[0]["ua"] == "bionico-agent/1.0"


def test_set_status_posts_error(server):
    Stub.responses["POST /api/agent/jobs/abc/status"] = (200, b"{}")
    make_client(server).set_status("abc", "failed", error="tail")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"status": "failed", "error": "tail"}


def test_set_status_posts_stage(server):
    Stub.responses["POST /api/agent/jobs/abc/status"] = (200, b"{}")
    make_client(server).set_status("abc", "processing", stage="render")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"status": "processing", "stage": "render"}


def test_set_status_omits_stage_and_error_when_none(server):
    Stub.responses["POST /api/agent/jobs/abc/status"] = (200, b"{}")
    make_client(server).set_status("abc", "canceled")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"status": "canceled"}


def test_post_metrics_posts_given_payload(server):
    Stub.responses["POST /api/agent/metrics"] = (200, b"{}")
    make_client(server).post_metrics({"five_hour_pct": 12, "seven_day_pct": 3})
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"five_hour_pct": 12, "seven_day_pct": 3}


def test_complete_posts_empty_body_by_default(server):
    Stub.responses["POST /api/agent/jobs/abc/complete"] = (200, b"{}")
    make_client(server).complete("abc")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {}


def test_complete_posts_given_payload(server):
    Stub.responses["POST /api/agent/jobs/abc/complete"] = (200, b"{}")
    make_client(server).complete("abc", {"run_id": "7_short", "outputs": []})
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"run_id": "7_short", "outputs": []}


def test_reclaims_returns_run_ids(server):
    Stub.responses["GET /api/agent/reclaims"] = (
        200, json.dumps({"run_ids": ["7_short", "8_short"]}).encode())
    assert make_client(server).reclaims() == ["7_short", "8_short"]


def test_reclaims_empty_on_missing_key(server):
    Stub.responses["GET /api/agent/reclaims"] = (200, b"{}")
    assert make_client(server).reclaims() == []


def test_ack_reclaims_posts_run_ids(server):
    Stub.responses["POST /api/agent/reclaims/ack"] = (200, b"{}")
    make_client(server).ack_reclaims(["7_short", "8_short"])
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"run_ids": ["7_short", "8_short"]}


def test_upload_url_posts_name(server):
    Stub.responses["POST /api/agent/jobs/abc/upload-url"] = (
        200, json.dumps({"key": "outbox/abc/final.mp4", "url": "http://x/put"}).encode())
    got = make_client(server).upload_url("abc", "final.mp4")
    sent = json.loads(Stub.calls[0]["body"])
    assert sent == {"name": "final.mp4"}
    assert got == {"key": "outbox/abc/final.mp4", "url": "http://x/put"}


def test_download_streams_to_dest(server, tmp_path):
    Stub.responses["GET /file"] = (200, b"MP4BYTES")
    dest = tmp_path / "in.mp4"
    make_client(server).download(f"{server}/file", dest)
    assert dest.read_bytes() == b"MP4BYTES"
    assert Stub.calls[0]["ua"] == "bionico-agent/1.0"


def test_download_raises_on_truncated_body(server, tmp_path):
    Stub.responses["GET /truncated"] = (200, b"MP4BYTES")
    Stub.content_length_override["GET /truncated"] = 999  # declared larger than body sent
    dest = tmp_path / "in.mp4"
    with pytest.raises(IOError, match="truncated"):
        make_client(server).download(f"{server}/truncated", dest)


def test_upload_sends_content_length(server, tmp_path):
    src = tmp_path / "out.mp4"
    src.write_bytes(b"RESULT")
    Stub.responses["PUT /put"] = (200, b"")
    make_client(server).upload_file(f"{server}/put", src)
    assert Stub.calls[0]["length"] == "6"
    assert Stub.calls[0]["body"] == b"RESULT"
    assert Stub.calls[0]["content_type"] == "video/mp4"


def test_upload_sends_explicit_content_type(server, tmp_path):
    src = tmp_path / "slide1.png"
    src.write_bytes(b"PNGDATA")
    Stub.responses["PUT /put-png"] = (200, b"")
    make_client(server).upload_file(f"{server}/put-png", src, content_type="image/png")
    assert Stub.calls[0]["content_type"] == "image/png"
