"""WS2: slide -> voice-span mapping (agent half of span_map).

The agent path is smoke-tested live; here we lock the deterministic contract:
span validation (clamp / widen / order / count) and the proportional fallback,
plus that `map_slides_to_spans` degrades to the fallback on any agent trouble
and passes validated agent spans through on success.
"""
from contenido_bionico.short.formats import span_map
from contenido_bionico.short.formats.span_map import (
    _proportional_spans,
    _transcript_duration,
    _validate_spans,
    _word_rows,
    map_slides_to_spans,
)

W = lambda t, s, e, ty="word": {"text": t, "start": s, "end": e, "type": ty}
# 20s of spoken words (plus a spacing row that must be ignored).
TRANSCRIPT = {
    "words": [
        W("Hoy", 0.0, 1.0),
        W(" ", 1.0, 1.1, ty="spacing"),
        W("hablamos", 1.1, 4.0),
        W("de", 4.0, 8.0),
        W("ventas", 8.0, 14.0),
        W("hoy", 14.0, 20.0),
    ]
}


# --- word extraction + duration -------------------------------------------

def test_word_rows_drops_spacing_and_keeps_order():
    rows = _word_rows(TRANSCRIPT)
    assert [r["text"] for r in rows] == ["Hoy", "hablamos", "de", "ventas", "hoy"]


def test_transcript_duration_is_last_word_end():
    assert _transcript_duration(_word_rows(TRANSCRIPT)) == 20.0


def test_word_rows_handles_missing_transcript():
    assert _word_rows(None) == []
    assert _word_rows({}) == []


# --- proportional fallback -------------------------------------------------

def test_proportional_spans_equal_split():
    assert _proportional_spans(4, 20.0) == [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0), (15.0, 20.0)]


def test_proportional_spans_empty():
    assert _proportional_spans(0, 20.0) == []
    assert _proportional_spans(3, 0.0) == []


# --- validation ------------------------------------------------------------

def test_validate_passthrough_valid_spans():
    raw = [{"i": 1, "start": 0.0, "end": 5.0}, {"i": 2, "start": 6.0, "end": 12.0}]
    assert _validate_spans(raw, 2, 20.0) == [(0.0, 5.0), (6.0, 12.0)]


def test_validate_wrong_count_returns_none():
    raw = [{"start": 0.0, "end": 5.0}]
    assert _validate_spans(raw, 2, 20.0) is None


def test_validate_not_a_list_returns_none():
    assert _validate_spans({"start": 0, "end": 1}, 1, 20.0) is None


def test_validate_clamps_out_of_range_to_timeline():
    raw = [{"start": -3.0, "end": 5.0}, {"start": 15.0, "end": 40.0}]
    out = _validate_spans(raw, 2, 20.0)
    assert out == [(0.0, 5.0), (15.0, 20.0)]


def test_validate_widens_short_span_to_min_len():
    # 0.4s span -> widened symmetrically to >= 1.2s.
    out = _validate_spans([{"start": 5.0, "end": 5.4}], 1, 20.0)
    assert out is not None
    (start, end), = out
    assert round(end - start, 3) >= 1.2
    # Symmetric around the original centre (5.2).
    assert abs(((start + end) / 2) - 5.2) < 1e-6


def test_validate_widen_clamps_at_zero_edge():
    # A tiny span at t=0 widens rightward only (can't go below 0).
    out = _validate_spans([{"start": 0.0, "end": 0.2}], 1, 20.0)
    assert out is not None
    (start, end), = out
    assert start == 0.0
    assert round(end - start, 3) >= 1.2


def test_validate_non_decreasing_violation_returns_none():
    raw = [{"start": 10.0, "end": 14.0}, {"start": 2.0, "end": 6.0}]
    assert _validate_spans(raw, 2, 20.0) is None


def test_validate_non_numeric_returns_none():
    assert _validate_spans([{"start": "x", "end": 5.0}], 1, 20.0) is None


def test_validate_missing_keys_returns_none():
    assert _validate_spans([{"start": 1.0}], 1, 20.0) is None


# --- end-to-end mapping (agent monkeypatched) ------------------------------

def test_map_uses_validated_agent_spans(monkeypatch):
    monkeypatch.setattr(
        span_map,
        "_agent_spans",
        lambda vid, slides, words, dur: [
            {"i": 1, "start": 0.0, "end": 6.0},
            {"i": 2, "start": 7.0, "end": 13.0},
        ],
    )
    out = map_slides_to_spans(["uno", "dos"], TRANSCRIPT, "9102_short")
    assert out == [(0.0, 6.0), (7.0, 13.0)]


def test_map_falls_back_when_agent_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no agent CLI")

    monkeypatch.setattr(span_map, "_agent_spans", boom)
    out = map_slides_to_spans(["uno", "dos"], TRANSCRIPT, "9102_short")
    # Proportional split of [0, 20] into 2.
    assert out == [(0.0, 10.0), (10.0, 20.0)]


def test_map_falls_back_when_agent_returns_wrong_count(monkeypatch):
    monkeypatch.setattr(
        span_map, "_agent_spans", lambda *a, **k: [{"i": 1, "start": 0.0, "end": 6.0}]
    )
    out = map_slides_to_spans(["uno", "dos", "tres"], TRANSCRIPT, "9102_short")
    assert out == _proportional_spans(3, 20.0)


def test_map_empty_slides_returns_empty(monkeypatch):
    monkeypatch.setattr(span_map, "_agent_spans", lambda *a, **k: [])
    assert map_slides_to_spans([], TRANSCRIPT, "9102_short") == []


def test_map_no_transcript_duration_falls_back(monkeypatch):
    monkeypatch.setattr(span_map, "_agent_spans", lambda *a, **k: [])
    # No spoken words -> duration 0 -> proportional of 0 duration -> [].
    assert map_slides_to_spans(["uno"], {"words": []}, "9102_short") == []
