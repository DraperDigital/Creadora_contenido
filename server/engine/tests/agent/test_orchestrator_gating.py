import sys
from pathlib import Path

from bionico import orchestrator
from bionico.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(repo_root=tmp_path, data_dir=tmp_path / "data")


def test_no_cloud_env_means_no_agent_child(tmp_path):
    names = [s["name"] for s in orchestrator.child_specs(_cfg(tmp_path))]
    assert names == ["watcher"]


def test_cloud_env_adds_agent_child(tmp_path):
    env = tmp_path / "server" / "cloud" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("DASHBOARD_BASE_URL=https://x.workers.dev\nAGENT_TOKEN=t\n", encoding="utf-8")
    specs = {s["name"]: s for s in orchestrator.child_specs(_cfg(tmp_path))}
    assert "agent" in specs
    assert specs["agent"]["cmd"] == [sys.executable, "-m", "bionico.agent.agent"]


def test_reaper_classifies_agent(tmp_path):
    cfg = _cfg(tmp_path)
    # argv must carry a repo-rooted path: the reaper only matches this
    # repo's own daemons (see _repo_scoped).
    py = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert orchestrator._classify_orphan(
        [py, "-m", "bionico.agent.agent"], cfg) == "agent"


def test_classify_orphan_is_repo_scoped(tmp_path):
    from bionico import orchestrator
    cfg = _cfg(tmp_path)
    other = [r"C:\Other\Repo\.venv\Scripts\python.exe", "-m", "bionico.watcher.watcher"]
    assert orchestrator._classify_orphan(other, cfg) is None
    mine = [str(tmp_path / ".venv" / "Scripts" / "python.exe"), "-m", "bionico.watcher.watcher"]
    assert orchestrator._classify_orphan(mine, cfg) == "watcher"
    dash = ["node", str(tmp_path / "server" / "local" / "server.mjs")]
    assert orchestrator._classify_orphan(dash, cfg) == "dashboard"


def test_child_specs_includes_dashboard_when_available(monkeypatch, tmp_path):
    from bionico import localdash
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(localdash, "available", lambda root: True)
    monkeypatch.setattr(localdash, "bootstrap", lambda root: None)
    names = [s["name"] for s in orchestrator.child_specs(cfg)]
    assert "dashboard" in names
    assert names.index("watcher") == 0


def test_child_specs_no_dashboard_without_server(monkeypatch, tmp_path):
    from bionico import localdash
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(localdash, "available", lambda root: False)
    names = [s["name"] for s in orchestrator.child_specs(cfg)]
    assert "dashboard" not in names
