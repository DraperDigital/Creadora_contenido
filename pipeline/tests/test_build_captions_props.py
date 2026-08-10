"""Unit tests for `build_captions_props` (Task 2 of the Lego-intermediates
change architecture).

`build_captions_props` is the pure/deterministic single source of truth for
the captions_props payload: chunk_cues + brand font + color + fontPx,
re-derived from a run's `transcript.json` + `source.mp4` duration, with the
optional `Captions_Style.json` color override applied. No agent calls, no
Remotion/ffmpeg render — only `_ffprobe_duration` is monkeypatched (probing a
real file is the one non-pure seam, and the fixture stubs it out).
"""
from dataclasses import dataclass
from pathlib import Path

import pytest

import contenido_bionico.short.animate.orchestrator as orch


@dataclass
class TmpRun:
    video_id: int
    run_dir: Path


@pytest.fixture
def tmp_run(tmp_path, monkeypatch):
    """A fake run dir under a sandboxed `RUNS` root (never the real repo
    `runs/`), pre-populated with a minimal transcript + a stub source.mp4.
    """
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(orch, "RUNS", runs_root)

    video_id = 999001
    run_dir = runs_root / str(video_id)
    run_dir.mkdir()
    # Content is irrelevant: `_ffprobe_duration` is monkeypatched per-test, so
    # this file only needs to exist for the `source.exists()` gate.
    (run_dir / "source.mp4").write_bytes(b"")

    words = [
        {"type": "word", "word": "hola", "start": 0.0, "end": 0.4},
        {"type": "word", "word": "mundo", "start": 0.4, "end": 0.9},
        {"type": "word", "word": "bionico", "start": 0.9, "end": 1.5},
        {"type": "word", "word": "video", "start": 1.5, "end": 2.0},
    ]
    (run_dir / "transcript.json").write_text(
        '{"words": %s}' % _words_json(words), encoding="utf-8"
    )

    monkeypatch.setattr(orch, "_ffprobe_duration", lambda p: 12.0)
    return TmpRun(video_id=video_id, run_dir=run_dir)


def _words_json(words: list[dict]) -> str:
    import json
    return json.dumps(words)


def test_build_captions_props_shape(tmp_run):
    props = orch.build_captions_props(tmp_run.video_id)
    assert props is not None
    assert props["durationSec"] == 12.0
    assert props["placement"] == "center"
    assert props["brand"]["font"] == orch.CAPTION_FALLBACK_FONT
    assert props["fontPx"] == orch.captions.SHORT_CAPTION_FONT_PX
    assert props["cues"]  # non-empty from the fixture transcript
    assert props["color"] is None  # no Captions_Style.json override present


def test_color_override(tmp_run):
    (tmp_run.run_dir / orch.CAPTIONS_STYLE_FILE).write_text(
        '{"color": "#ffff00"}', encoding="utf-8"
    )
    props = orch.build_captions_props(tmp_run.video_id)
    assert props is not None
    assert props["color"] == "#ffff00"
    assert props["placement"] == "center"
    assert props["cues"]


def test_style_tokens_headline_font_is_picked_up(tmp_run):
    (tmp_run.run_dir / orch.STYLE_TOKENS_FILE).write_text(
        '{"fonts": {"headline": "Inter"}}', encoding="utf-8"
    )
    props = orch.build_captions_props(tmp_run.video_id)
    assert props is not None
    assert props["brand"]["font"] == "Inter"


def test_style_tokens_without_headline_font_uses_tokens_default(tmp_run):
    (tmp_run.run_dir / orch.STYLE_TOKENS_FILE).write_text(
        '{"palette": {"accent": "#ff0000"}}', encoding="utf-8"
    )
    props = orch.build_captions_props(tmp_run.video_id)
    assert props is not None
    assert props["brand"]["font"] == orch.CAPTION_TOKENS_DEFAULT_FONT


def test_no_transcript_returns_none(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(orch, "RUNS", runs_root)
    video_id = 999002
    run_dir = runs_root / str(video_id)
    run_dir.mkdir()
    (run_dir / "source.mp4").write_bytes(b"")
    # No transcript.json written.
    assert orch.build_captions_props(video_id) is None


def test_no_words_returns_none(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(orch, "RUNS", runs_root)
    video_id = 999003
    run_dir = runs_root / str(video_id)
    run_dir.mkdir()
    (run_dir / "source.mp4").write_bytes(b"")
    (run_dir / "transcript.json").write_text('{"words": []}', encoding="utf-8")
    monkeypatch.setattr(orch, "_ffprobe_duration", lambda p: 12.0)
    assert orch.build_captions_props(video_id) is None
