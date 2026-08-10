"""Unit tests for the segment math in ``shared/cut/make_render_edl.py``.

Covers the two conversion modes (plain keep-ranges and --keep-silences word-cut
complement) plus the silence/word classification helper. Pure JSON-in/JSON-out;
no ffmpeg, no network.
"""
import json
import sys

import pytest

from contenido_bionico.shared.cut import make_render_edl as mre


# ---------- _has_words ----------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("hola mundo", True),
        ("", False),
        ("   ", False),
        (None, False),
        ("[silence]", False),
        ("[pause]", False),
        ("  [silencio]  ", False),
        ("[inicio] pero sigue", True),  # bracket only at the start = speech
    ],
)
def test_has_words(text, expected):
    assert mre._has_words(text) is expected


# ---------- _word_cut_ranges (keep-silences mode) ----------

def test_word_cut_ranges_preserves_silence_cuts():
    segments = [
        {"start": 0.0, "end": 2.0, "status": "keep", "text": "hola"},
        {"start": 2.0, "end": 3.0, "status": "cut", "text": "[silence]"},
        {"start": 3.0, "end": 5.0, "status": "keep", "text": "sigue"},
    ]
    ranges = mre._word_cut_ranges(segments, "v.mp4")
    # The silence cut is NOT removed: one continuous span first-keep..last-keep.
    assert ranges == [{"source": "v.mp4", "start": 0.0, "end": 5.0}]


def test_word_cut_ranges_removes_only_spoken_cuts():
    segments = [
        {"start": 0.0, "end": 2.0, "status": "keep", "text": "hola"},
        {"start": 2.0, "end": 3.0, "status": "cut", "text": "eh este"},
        {"start": 3.0, "end": 5.0, "status": "keep", "text": "sigue"},
    ]
    ranges = mre._word_cut_ranges(segments, "v.mp4")
    assert ranges == [
        {"source": "v.mp4", "start": 0.0, "end": 2.0},
        {"source": "v.mp4", "start": 3.0, "end": 5.0},
    ]


def test_word_cut_ranges_merges_overlapping_word_cuts():
    segments = [
        {"start": 0.0, "end": 1.0, "status": "keep", "text": "a"},
        {"start": 1.0, "end": 2.5, "status": "cut", "text": "eh"},
        {"start": 2.0, "end": 3.0, "status": "cut", "text": "este"},
        {"start": 3.0, "end": 6.0, "status": "keep", "text": "b"},
    ]
    ranges = mre._word_cut_ranges(segments, "v.mp4")
    assert ranges == [
        {"source": "v.mp4", "start": 0.0, "end": 1.0},
        {"source": "v.mp4", "start": 3.0, "end": 6.0},
    ]


def test_word_cut_ranges_clips_cuts_outside_kept_span():
    # A spoken cut BEFORE the first keep is irrelevant: span starts at the
    # first kept second anyway.
    segments = [
        {"start": 0.0, "end": 1.0, "status": "cut", "text": "intro descartada"},
        {"start": 1.0, "end": 4.0, "status": "keep", "text": "hola"},
    ]
    ranges = mre._word_cut_ranges(segments, "v.mp4")
    assert ranges == [{"source": "v.mp4", "start": 1.0, "end": 4.0}]


def test_word_cut_ranges_no_keeps_returns_empty():
    segments = [{"start": 0.0, "end": 1.0, "status": "cut", "text": "x"}]
    assert mre._word_cut_ranges(segments, "v.mp4") == []


# ---------- main() plain keep mode ----------

def _run_main(monkeypatch, tmp_path, edl, extra_args=()):
    src = tmp_path / "edl_trimmed.json"
    out = tmp_path / "edl_render.json"
    src.write_text(json.dumps(edl), encoding="utf-8")
    argv = ["make_render_edl.py", str(src), "-o", str(out), *extra_args]
    monkeypatch.setattr(sys, "argv", argv)
    rc = mre.main()
    return rc, out


def test_main_keeps_only_keep_segments(monkeypatch, tmp_path):
    edl = {
        "source": "v.mp4",
        "segments": [
            {"start": 0.0, "end": 5.2, "status": "keep", "text": "a"},
            {"start": 5.2, "end": 5.8, "status": "cut", "text": "[silence]"},
            {"start": 5.8, "end": 9.0, "status": "keep", "text": "b"},
        ],
    }
    rc, out = _run_main(monkeypatch, tmp_path, edl)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload == {
        "source": "v.mp4",
        "ranges": [
            {"source": "v.mp4", "start": 0.0, "end": 5.2},
            {"source": "v.mp4", "start": 5.8, "end": 9.0},
        ],
    }


def test_main_skips_non_positive_duration_keep(monkeypatch, tmp_path, capsys):
    edl = {
        "source": "v.mp4",
        "segments": [
            {"start": 2.0, "end": 2.0, "status": "keep", "text": "vacio"},
            {"start": 3.0, "end": 4.0, "status": "keep", "text": "ok"},
        ],
    }
    rc, out = _run_main(monkeypatch, tmp_path, edl)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ranges"] == [{"source": "v.mp4", "start": 3.0, "end": 4.0}]
    assert "non-positive duration" in capsys.readouterr().err


def test_main_errors_when_no_keeps(monkeypatch, tmp_path):
    edl = {
        "source": "v.mp4",
        "segments": [{"start": 0.0, "end": 1.0, "status": "cut", "text": "x"}],
    }
    rc, out = _run_main(monkeypatch, tmp_path, edl)
    assert rc == 2
    assert not out.exists()


def test_main_keep_silences_flag_uses_word_cut_complement(monkeypatch, tmp_path):
    edl = {
        "source": "v.mp4",
        "segments": [
            {"start": 0.0, "end": 2.0, "status": "keep", "text": "hola"},
            {"start": 2.0, "end": 3.0, "status": "cut", "text": "[silence]"},
            {"start": 3.0, "end": 5.0, "status": "keep", "text": "sigue"},
        ],
    }
    rc, out = _run_main(monkeypatch, tmp_path, edl, extra_args=("--keep-silences",))
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    # Silence between keeps survives: one continuous range.
    assert payload["ranges"] == [{"source": "v.mp4", "start": 0.0, "end": 5.0}]
