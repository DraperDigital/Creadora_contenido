"""Tests for Task 8 — registry-driven change agent + UTF-8 env plumbing.

Three seams are covered, none of which launches a real Claude agent or a real
Remotion render:

1. The additive `env` param actually reaches the subprocess layer, and (for
   Claude) LAYERS OVER the runner's own `child_env` instead of replacing it.
   `start_process` is monkeypatched to capture its `env` kwarg.
2. `run_change` launches the agent with the UTF-8 env
   (`PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`), a bounded `MAX_TURNS`, the
   UNCHANGED `MODEL`/`EFFORT`, and a `registry` block in the initial message;
   the `RESULT:` parse + publish safety net still work. `run_agent_code` is
   monkeypatched (as imported into `short.change.orchestrator`) to capture
   kwargs and return a canned result.
3. Doc-lint on `change_orchestrator.md`: the old render recipes A-E are gone
   and the registry + two primitives + the run-dir-only rule are taught, so
   the recipes can't silently come back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import contenido_bionico.short.change.orchestrator as change_orch


# ---------------------------------------------------------------------------
# 1. env threading reaches start_process (+ layers over child_env for Claude)
# ---------------------------------------------------------------------------


class _FakeStdin:
    def write(self, _text):  # pragma: no cover - trivial
        pass

    def close(self):  # pragma: no cover - trivial
        pass


class _FakeStream:
    """A pipe whose readline()/read() EOF immediately so the drain threads
    finish at once and the runner returns without a real subprocess."""

    def readline(self):
        return ""

    def read(self, _n=-1):
        return ""

    def close(self):  # pragma: no cover - trivial
        pass


class _FakeProc:
    def __init__(self):
        self.pid = 4321
        self.returncode = 0
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    def wait(self, timeout=None):
        return 0

    def kill(self):  # pragma: no cover - trivial
        pass


def _install_fake_start_process(monkeypatch, target_module, captured: dict):
    """Patch `start_process` as imported into `target_module` to capture the
    env it is called with and return an already-finished fake process."""

    def _fake_start_process(cmd, *, cwd, env=None, stdin=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(target_module, "start_process", _fake_start_process)
    # kill_process_tree walks the real OS process tree by pid; neutralize it so
    # the fake pid can't match a real process.
    monkeypatch.setattr(target_module, "kill_process_tree", lambda proc: None)


def test_claude_env_layers_over_child_env(monkeypatch):
    """`run_claude_code(env=...)` must MERGE the caller env on top of the
    runner's own `child_env` (which sets CLAUDE_CODE_MAX_OUTPUT_TOKENS and
    strips harness vars), not replace it. So the captured subprocess env must
    contain BOTH the caller's PYTHONUTF8 AND the runner's
    CLAUDE_CODE_MAX_OUTPUT_TOKENS."""
    import contenido_bionico.shared.runtime.claude_runner as claude_runner

    captured: dict = {}
    _install_fake_start_process(monkeypatch, claude_runner, captured)

    claude_runner.run_claude_code(
        claude_cmd="claude",
        system_prompt="sys",
        initial_message="hi",
        cwd=Path.cwd(),
        timeout_seconds=5,
        max_turns=3,
        tools=["Read"],
        model="claude-opus-4-8",
        effort="high",
        log_path=None,
        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )

    env = captured["env"]
    assert env is not None
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    # Proof it layered OVER child_env instead of replacing it:
    assert env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "128000"


def test_claude_env_none_is_backward_compatible(monkeypatch):
    """With no env passed (existing callers), the runner still builds its
    child_env exactly as before — CLAUDE_CODE_MAX_OUTPUT_TOKENS present, no
    PYTHONUTF8 injected by us."""
    import contenido_bionico.shared.runtime.claude_runner as claude_runner

    captured: dict = {}
    _install_fake_start_process(monkeypatch, claude_runner, captured)

    claude_runner.run_claude_code(
        claude_cmd="claude",
        system_prompt="sys",
        initial_message="hi",
        cwd=Path.cwd(),
        timeout_seconds=5,
        max_turns=3,
        tools=["Read"],
        log_path=None,
    )

    env = captured["env"]
    # child_env is always built and passed (never None) by run_claude_code.
    assert env is not None
    assert env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "128000"


def test_codex_env_merges_over_os_environ(monkeypatch):
    """`run_codex_exec(env=...)` must merge over os.environ and reach
    start_process. codex builds no child_env of its own, so the merge is
    `{**os.environ, **env}`."""
    import os

    import contenido_bionico.shared.runtime.agent_runner as agent_runner

    monkeypatch.setenv("CB_WIRING_MARKER", "present")
    captured: dict = {}
    _install_fake_start_process(monkeypatch, agent_runner, captured)

    agent_runner.run_codex_exec(
        codex_cmd="codex",
        system_prompt="sys",
        initial_message="hi",
        cwd=Path.cwd(),
        timeout_seconds=5,
        max_turns=agent_runner.DEFAULT_MAX_TURNS,
        tools=["Read"],
        log_path=None,
        env={"PYTHONUTF8": "1"},
    )

    env = captured["env"]
    assert env is not None
    assert env["PYTHONUTF8"] == "1"
    # os.environ was merged under the overlay.
    assert env.get("CB_WIRING_MARKER") == "present"


def test_run_agent_code_forwards_env_to_claude(monkeypatch):
    """`run_agent_code(env=...)` threads env down to `run_claude_code`."""
    import contenido_bionico.shared.runtime.agent_runner as agent_runner

    monkeypatch.setattr(agent_runner.config, "agent_provider", lambda: "claude_code")

    seen: dict = {}

    def _fake_run_claude_code(**kwargs):
        seen.update(kwargs)

        class _R:
            returncode = 0
            stdout = "RESULT: {}"
            stderr = ""

        return _R()

    monkeypatch.setattr(agent_runner, "run_claude_code", _fake_run_claude_code)

    agent_runner.run_agent_code(
        agent_cmd="claude",
        system_prompt="s",
        initial_message="m",
        cwd=Path.cwd(),
        timeout_seconds=1,
        env={"PYTHONUTF8": "1"},
    )
    assert seen.get("env") == {"PYTHONUTF8": "1"}


def test_run_agent_code_forwards_env_to_codex(monkeypatch):
    """`run_agent_code(env=...)` threads env down to `run_codex_exec` too."""
    import contenido_bionico.shared.runtime.agent_runner as agent_runner

    monkeypatch.setattr(agent_runner.config, "agent_provider", lambda: "codex_cli")

    seen: dict = {}

    def _fake_run_codex_exec(**kwargs):
        seen.update(kwargs)

        class _R:
            returncode = 0
            stdout = "RESULT: {}"
            stderr = ""

        return _R()

    monkeypatch.setattr(agent_runner, "run_codex_exec", _fake_run_codex_exec)

    agent_runner.run_agent_code(
        agent_cmd="codex",
        system_prompt="s",
        initial_message="m",
        cwd=Path.cwd(),
        timeout_seconds=1,
        env={"PYTHONUTF8": "1"},
    )
    assert seen.get("env") == {"PYTHONUTF8": "1"}


# ---------------------------------------------------------------------------
# 2. run_change: UTF-8 env + registry block + bounded turns + model/effort
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_launch(monkeypatch, tmp_path):
    """Monkeypatch the agent launch + publish so run_change exercises only the
    pure orchestration seam. Returns the captured run_agent_code kwargs."""
    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = (
            'RESULT: {"changed": ["video"], '
            '"summary": "captions mas grandes y amarillas", "unsupported": null}'
        )
        stderr = ""

    def _fake_run_agent_code(**kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(change_orch, "run_agent_code", _fake_run_agent_code)
    monkeypatch.setattr(change_orch, "resolve_agent_cmd", lambda: "claude")

    # Sandbox the runs dir so log writes + inspect_run land in tmp.
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(change_orch, "RUNS_DIR", runs_root)
    rd = runs_root / "7_short"
    rd.mkdir(parents=True)

    # Neutralize the publish safety net (no real pipeline).
    import contenido_bionico.pipeline as pl

    monkeypatch.setattr(pl, "publish_output", lambda *a, **k: None)
    monkeypatch.setattr(pl, "run_output_dir", lambda vid: None)

    return captured


def test_run_change_passes_utf8_env(captured_launch):
    result = change_orch.run_change("7_short", "video", "haz los subtitulos amarillos")
    env = captured_launch["env"]
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    # normalized return shape survives.
    assert result["changed"] == ["video"]
    assert result["unsupported"] is None
    assert "captions" in result["summary"]


def test_run_change_bounds_max_turns_and_keeps_model_effort(captured_launch):
    change_orch.run_change("7_short", "video", "sube el volumen de la musica")
    assert captured_launch["max_turns"] == 40
    assert captured_launch["max_turns"] <= 60  # bounded
    assert captured_launch["timeout_seconds"] == 60 * 60
    # The whole point: the cost fix is NOT the model/effort.
    assert captured_launch["model"] == "claude-opus-4-8"
    assert captured_launch["effort"] == "high"


def test_run_change_injects_registry_block(captured_launch):
    change_orch.run_change("7_short", "video", "cambia la camara")
    message = captured_launch["initial_message"]
    assert '"registry"' in message
    # Only the target's parts are surfaced (video excludes carousel/quotes).
    assert '"captions"' in message
    assert '"carousel"' not in message


def test_run_change_registry_block_is_target_scoped_for_carousel(captured_launch):
    change_orch.run_change("7_short", "carousel", "reescribe la slide 2")
    message = captured_launch["initial_message"]
    assert '"registry"' in message
    assert '"carousel"' in message
    # video-only parts must not leak into a carousel change.
    assert '"captions"' not in message


def test_run_change_result_parse_and_publish_intact(captured_launch, monkeypatch):
    """The RESULT line is parsed and the publish safety net is invoked for a
    changed video."""
    calls: list = []
    import contenido_bionico.pipeline as pl

    # A final.mp4 present so the safety net actually attempts a publish.
    rd = change_orch.RUNS_DIR / "7_short"
    (rd / "final.mp4").write_bytes(b"x")
    monkeypatch.setattr(
        pl, "publish_output", lambda *a, **k: calls.append((a, k))
    )
    result = change_orch.run_change("7_short", "video", "haz los subtitulos amarillos")
    assert result == {
        "changed": ["video"],
        "summary": "captions mas grandes y amarillas",
        "unsupported": None,
    }
    assert calls, "publish safety net should have run for a changed video"


# ---------------------------------------------------------------------------
# 3. Doc-lint: recipes gone, registry + primitives + hard rule present
# ---------------------------------------------------------------------------


def _md_text() -> str:
    return change_orch.PROMPT_PATH.read_text(encoding="utf-8")


def test_md_teaches_primitives_not_recipes():
    md = _md_text()
    assert "rerender_part" in md
    assert "recompose" in md
    # The old recipe section + its headers must be gone.
    assert "RE-RENDER RECIPES" not in md
    assert "### A." not in md
    assert "### E." not in md


def test_md_has_run_dir_only_hard_rule():
    md = _md_text().lower()
    assert "shared/" in md  # the hard rule references it
    assert "never edit" in md


def test_md_inlines_registry_and_two_tier_captions():
    md = _md_text()
    # Registry table inlined so the agent never reads registry.py.
    assert "registry.py" in md  # referenced as "do not read"
    # Two-tier caption mechanism named.
    assert "copy_shared_component_tsx" in md
    assert "captions_props.json" in md


def test_md_teaches_render_once_and_no_frame_reading():
    """Task 10 fix: the agent used to eyeball caption sizing by extracting +
    Reading PNG frames (which return nothing in this environment) and
    re-rendering repeatedly (fontPx 80->76->70) to second-guess overflow.
    The .md must now explicitly steer away from both: verify via
    intermediate values (not extracted frames), and render each deliverable
    exactly once."""
    md = _md_text()
    lower = md.lower()
    # (a) Don't verify by reading extracted image/PNG frames.
    assert "png" in lower
    assert "nothing in this environment" in lower or "returns nothing" in lower
    # (b) Render once; chunk_cues auto-wraps so a bigger fontPx won't overflow.
    assert "render each deliverable once" in lower
    assert "chunk_cues" in md
    assert "auto-wrap" in lower


def test_md_forbids_background_monitors():
    md = _md_text()
    lower = md.lower()
    assert "run_in_background: true" in md
    assert "foreground" in lower
    assert "monitor" in lower
    assert "i'll wait" in lower


def test_md_routes_cut_change_to_surgical_recut():
    """Cut edits are supported through the pinpointed surgical helper, not the
    older full re-derive path.
    """
    md = _md_text()
    assert "surgical_recut" in md
    assert "recut_run" not in md
    lower = md.lower()
    assert "surgical" in lower
    assert "pinpointed" in lower
    # The deletion-only add-content case is still reported as unsupported.
    assert "unsupported" in lower
