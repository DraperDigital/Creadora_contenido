"""Shared fixtures for pipeline/tests.

Task 1 (characterization safety net) provides `remotion_src_sandbox`, which
every later refactor task's tests should reuse whenever they exercise
`remotion_manifest_writer` / `remotion_manifest_short` codegen — it keeps
tests from ever writing into the real `shared/remotion/src/runs/`.
"""
import importlib
import pytest


@pytest.fixture
def remotion_src_sandbox(tmp_path, monkeypatch):
    """Point REMOTION_SRC (writer + short wrapper) at a tmp dir."""
    writer = importlib.import_module("contenido_bionico.shared.remotion_manifest_writer")
    short = importlib.import_module("contenido_bionico.shared.remotion_manifest_short")
    sandbox = tmp_path / "remotion_src"
    (sandbox / "runs").mkdir(parents=True)
    monkeypatch.setattr(writer, "REMOTION_SRC", sandbox)
    monkeypatch.setattr(short, "REMOTION_SRC", sandbox)
    return sandbox
