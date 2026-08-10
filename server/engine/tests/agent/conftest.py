import pytest

from bionico.agent import agent as _agent


@pytest.fixture(autouse=True)
def _bypass_preflight(monkeypatch):
    """The bridge tests feed fake bytes (b"VIDEO") as downloads; running the
    real ffprobe preflight against them would reject every delivery. Preflight
    has its own dedicated tests against the preflight module."""
    monkeypatch.setattr(_agent, "_preflight", lambda client, job, tmp: tmp)
