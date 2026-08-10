"""Paths and configuration for Contenido Bionico V5.

The orchestrator config lives at <repo>/.bionico/config.json. Runtime data
defaults to <repo>/server/engine/runtime and is overridable via BIONICO_DATA_DIR.
Secrets live in <repo>/.env (never committed).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def repo_root_default() -> Path:
    # server/engine/bionico/config.py -> repo root is four parents up.
    return Path(__file__).resolve().parents[3]


@dataclass
class Config:
    repo_root: Path
    data_dir: Path
    pipeline_dir: Path | None = None

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def inbox_bionico_dir(self) -> Path:
        return self.inbox_dir / "bionico"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "_work"

    @property
    def config_path(self) -> Path:
        return self.repo_root / ".bionico" / "config.json"

    @property
    def env_path(self) -> Path:
        return self.repo_root / ".env"

    def ensure_dirs(self) -> None:
        for p in (self.inbox_bionico_dir, self.work_dir):
            p.mkdir(parents=True, exist_ok=True)


def _data_dir(repo_root: Path) -> Path:
    env = os.environ.get("BIONICO_DATA_DIR")
    # config.py lives at server/engine/bionico/config.py; the runtime queue+work
    # area is at server/engine/runtime (parents[1] of this file).
    return Path(env) if env else Path(__file__).resolve().parents[1] / "runtime"


def load(repo_root: Path | None = None) -> Config:
    repo_root = (repo_root or repo_root_default()).resolve()
    data_dir = _data_dir(repo_root).resolve()
    cfg = Config(
        repo_root=repo_root,
        data_dir=data_dir,
        pipeline_dir=repo_root / "pipeline",
    )
    p = cfg.config_path
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("pipeline_dir"):
            cfg.pipeline_dir = Path(raw["pipeline_dir"])
    return cfg


def save(cfg: Config) -> None:
    cfg.config_path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "pipeline_dir": str(cfg.pipeline_dir) if cfg.pipeline_dir else None,
    }
    tmp = cfg.config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(cfg.config_path)


def load_env(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out
