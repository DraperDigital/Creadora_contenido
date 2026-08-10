"""HTTP client for the cloud dashboard's agent API (stdlib urllib only)."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from bionico.agent.cloudcfg import CloudConfig

CHUNK = 1 << 20  # 1 MiB
USER_AGENT = "bionico-agent/1.0"

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".zip": "application/zip",
    ".srt": "text/plain",
    ".pdf": "application/pdf",
}


class CloudClient:
    def __init__(self, cfg: CloudConfig):
        self._base = cfg.base_url
        self._token = cfg.agent_token

    def _api(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"Authorization": "Bearer %s" % self._token,
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status == 204:
                return None
            body = r.read()
            return json.loads(body) if body else None

    def claim(self) -> dict | None:
        return self._api("POST", "/api/agent/claim")

    def active_jobs(self) -> list[dict]:
        return self._api("GET", "/api/agent/jobs") or []

    def set_status(self, job_id: str, status: str, error: str | None = None,
                   stage: str | None = None, stage_detail: dict | None = None) -> None:
        payload = {"status": status}
        if error is not None:
            payload["error"] = error
        if stage is not None:
            payload["stage"] = stage
        if stage_detail is not None:
            # e.g. {"scene": n, "total": t}; the Worker stores it as TEXT and
            # the dashboard renders "Animando escena n de t".
            payload["stage_detail"] = stage_detail
        self._api("POST", "/api/agent/jobs/%s/status" % job_id, payload)

    def post_metrics(self, payload: dict) -> None:
        self._api("POST", "/api/agent/metrics", payload)

    def reclaims(self) -> list[str]:
        """Run ids of deleted versions awaiting local disk reclaim."""
        data = self._api("GET", "/api/agent/reclaims")
        return (data or {}).get("run_ids", []) if isinstance(data, dict) else []

    def ack_reclaims(self, run_ids: list[str]) -> None:
        """Drop the given run ids from the reclaim queue (disk already freed)."""
        self._api("POST", "/api/agent/reclaims/ack", {"run_ids": list(run_ids)})

    def upload_url(self, job_id: str, name: str) -> dict:
        return self._api("POST", "/api/agent/jobs/%s/upload-url" % job_id, {"name": name})

    def get_restart_control(self) -> dict | None:
        return self._api("GET", "/api/agent/control/restart")

    def ack_restart(self) -> None:
        self._api("POST", "/api/agent/control/restart/ack", {})

    def complete(self, job_id: str, payload: dict | None = None) -> None:
        self._api("POST", "/api/agent/jobs/%s/complete" % job_id, payload or {})

    def download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            expected = r.headers.get("Content-Length")
            written = 0
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        if expected is not None and int(expected) != written:
            raise IOError("truncated download: got %d of %s bytes" % (written, expected))

    def upload_file(self, url: str, src: Path, content_type: str | None = None) -> None:
        size = src.stat().st_size
        with open(src, "rb") as f:
            req = urllib.request.Request(
                url, data=f, method="PUT",
                headers={"Content-Length": str(size),
                         "Content-Type": content_type or "video/mp4",
                         "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=600):
                pass
