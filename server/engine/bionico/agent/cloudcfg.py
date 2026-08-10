"""Cloud dashboard credentials: <repo>/server/cloud/.env (gitignored)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bionico.config import load_env


@dataclass(frozen=True)
class CloudConfig:
    base_url: str
    agent_token: str


def cloud_env_path(repo_root: Path) -> Path:
    return Path(repo_root) / "server" / "cloud" / ".env"


def load_cloud(repo_root: Path) -> CloudConfig | None:
    values = load_env(cloud_env_path(repo_root))
    base_url = values.get("DASHBOARD_BASE_URL", "").strip().rstrip("/")
    token = values.get("AGENT_TOKEN", "").strip()
    if not base_url or not token:
        return None
    return CloudConfig(base_url=base_url, agent_token=token)
