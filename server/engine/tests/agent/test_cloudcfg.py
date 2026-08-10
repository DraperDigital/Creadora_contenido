from pathlib import Path

from bionico.agent.cloudcfg import CloudConfig, cloud_env_path, load_cloud


def _write(tmp_path: Path, text: str) -> Path:
    env = tmp_path / "server" / "cloud" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text(text, encoding="utf-8")
    return tmp_path


def test_missing_file_returns_none(tmp_path):
    assert load_cloud(tmp_path) is None


def test_incomplete_env_returns_none(tmp_path):
    root = _write(tmp_path, "DASHBOARD_BASE_URL=https://x.workers.dev\n")
    assert load_cloud(root) is None


def test_loads_and_strips_trailing_slash(tmp_path):
    root = _write(tmp_path,
                  "DASHBOARD_BASE_URL=https://x.workers.dev/\nAGENT_TOKEN=tok123\n")
    cfg = load_cloud(root)
    assert cfg == CloudConfig(base_url="https://x.workers.dev", agent_token="tok123")


def test_cloud_env_path(tmp_path):
    assert cloud_env_path(tmp_path) == tmp_path / "server" / "cloud" / ".env"
