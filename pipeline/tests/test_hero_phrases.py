"""Deterministic tests for hero-phrase validation, re-anchoring and fallback.

The agent call (`_select_phrases`) is monkeypatched everywhere — these tests
never spawn a subprocess. They pin the parts that guard correctness: verbatim
re-anchoring (times come from the matcher, never the agent), dropping
non-verbatim / wrong-length phrases, ≥2s spacing, the <4-survivors fallback, and
the caption-cue fallback shape.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from contenido_bionico.short.formats import hero_phrases as hp


def _w(text, start, end):
    return {"text": text, "start": start, "end": end, "type": "word"}


# ~18s transcript with five findable, well-spaced dramatic phrases.
WORDS = [
    _w("Hoy", 0.0, 0.4), _w("te", 0.4, 0.6), _w("explico", 0.6, 1.2),
    _w("vender", 2.0, 2.5), _w("mas", 2.5, 2.8), _w("rapido", 2.8, 3.4),
    _w("el", 5.0, 5.2), _w("secreto", 5.2, 5.8), _w("es", 5.8, 6.0), _w("simple", 6.0, 6.6),
    _w("nadie", 9.0, 9.4), _w("te", 9.4, 9.6), _w("lo", 9.6, 9.8), _w("dice", 9.8, 10.4),
    _w("empieza", 13.0, 13.6), _w("hoy", 13.6, 14.0), _w("mismo", 14.0, 14.6),
    _w("cambia", 17.0, 17.5), _w("tu", 17.5, 17.7), _w("vida", 17.7, 18.3),
]


def _ctx(tmp_path, transcript=None):
    return SimpleNamespace(run_dir=tmp_path, transcript=lambda: transcript)


# --- _validate_and_anchor -----------------------------------------------------

def test_validate_reanchors_times_from_words():
    # Agent supplies deliberately WRONG times; the matcher must overwrite them.
    phrases = [{"text": "vender mas rapido", "start": 99.0, "end": 100.0}]
    out = hp._validate_and_anchor(phrases, WORDS)
    assert out == [{"text": "vender mas rapido", "start": 2.0, "end": 3.4}]


def test_validate_drops_non_verbatim():
    phrases = [
        {"text": "vender mas rapido", "start": 0, "end": 0},
        {"text": "frase inventada que no existe", "start": 0, "end": 0},
        {"text": "nadie te lo dice", "start": 0, "end": 0},
    ]
    out = hp._validate_and_anchor(phrases, WORDS)
    texts = [p["text"] for p in out]
    assert "frase inventada que no existe" not in texts
    assert texts == ["vender mas rapido", "nadie te lo dice"]


def test_validate_drops_wrong_word_count():
    phrases = [
        {"text": "Hoy", "start": 0, "end": 0},                       # 1 word -> drop
        {"text": "Hoy te explico vender mas rapido el", "start": 0, "end": 0},  # 7 words -> drop
        {"text": "el secreto es simple", "start": 0, "end": 0},      # 4 words -> keep
    ]
    out = hp._validate_and_anchor(phrases, WORDS)
    assert [p["text"] for p in out] == ["el secreto es simple"]


def test_validate_enforces_spacing():
    # Two verbatim phrases whose anchored starts are <2s apart: keep only the
    # earlier one.
    phrases = [
        {"text": "el secreto", "start": 0, "end": 0},   # anchors at 5.0
        {"text": "es simple", "start": 0, "end": 0},     # anchors at 5.8 (<2s later)
    ]
    out = hp._validate_and_anchor(phrases, WORDS)
    assert [p["text"] for p in out] == ["el secreto"]


def test_validate_sorts_and_caps():
    phrases = [
        {"text": "cambia tu vida", "start": 0, "end": 0},
        {"text": "vender mas rapido", "start": 0, "end": 0},
        {"text": "nadie te lo dice", "start": 0, "end": 0},
    ]
    out = hp._validate_and_anchor(phrases, WORDS, max_phrases=2)
    assert [p["start"] for p in out] == sorted(p["start"] for p in out)
    assert [p["text"] for p in out] == ["vender mas rapido", "nadie te lo dice"]


# --- hero_phrases orchestration ----------------------------------------------

def test_hero_phrases_uses_agent_when_enough_survive(tmp_path, monkeypatch):
    agent = [
        {"text": "vender mas rapido", "start": 0, "end": 0},
        {"text": "el secreto es simple", "start": 0, "end": 0},
        {"text": "nadie te lo dice", "start": 0, "end": 0},
        {"text": "empieza hoy mismo", "start": 0, "end": 0},
        {"text": "cambia tu vida", "start": 0, "end": 0},
    ]
    monkeypatch.setattr(hp, "_select_phrases", lambda ctx, words: agent)
    called = {"fallback": False}
    monkeypatch.setattr(hp, "_fallback_phrases",
                        lambda *a, **k: called.__setitem__("fallback", True) or [])
    out = hp.hero_phrases(_ctx(tmp_path, {"words": WORDS}))
    assert len(out) == 5
    assert called["fallback"] is False
    assert out[0] == {"text": "vender mas rapido", "start": 2.0, "end": 3.4}


def test_hero_phrases_falls_back_when_few_survive(tmp_path, monkeypatch):
    # Only 2 verbatim survivors -> below the 4-floor -> fallback path.
    agent = [
        {"text": "vender mas rapido", "start": 0, "end": 0},
        {"text": "nadie te lo dice", "start": 0, "end": 0},
        {"text": "no existe en absoluto", "start": 0, "end": 0},
    ]
    monkeypatch.setattr(hp, "_select_phrases", lambda ctx, words: agent)
    sentinel = [{"text": "fallback frase", "start": 0.0, "end": 1.0}]
    monkeypatch.setattr(hp, "_fallback_phrases", lambda *a, **k: sentinel)
    out = hp.hero_phrases(_ctx(tmp_path, {"words": WORDS}))
    assert out is sentinel


def test_hero_phrases_falls_back_on_agent_error(tmp_path, monkeypatch):
    def boom(ctx, words):
        raise RuntimeError("agent down")

    monkeypatch.setattr(hp, "_select_phrases", boom)
    sentinel = [{"text": "fb", "start": 0.0, "end": 1.0}]
    monkeypatch.setattr(hp, "_fallback_phrases", lambda *a, **k: sentinel)
    out = hp.hero_phrases(_ctx(tmp_path, {"words": WORDS}))
    assert out is sentinel


def test_hero_phrases_empty_without_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "_select_phrases", lambda ctx, words: [])
    assert hp.hero_phrases(_ctx(tmp_path, None)) == []


# --- _fallback_phrases (caption cues) ----------------------------------------

def test_fallback_from_caption_cues(tmp_path):
    cues = [
        {"text": "vender mas rapido cada dia", "start": 2.0, "end": 3.4},
        {"text": "nadie te lo dice nunca", "start": 9.0, "end": 10.4},
        {"text": "empieza hoy mismo ahora", "start": 13.0, "end": 14.6},
        {"text": "cambia tu vida entera", "start": 17.0, "end": 18.3},
    ]
    (tmp_path / "captions_props.json").write_text(
        json.dumps({"cues": cues}), encoding="utf-8"
    )
    out = hp._fallback_phrases(_ctx(tmp_path, {"words": WORDS}), WORDS)
    assert len(out) == 4
    # First up-to-4 words of each cue, spaced, ordered.
    assert out[0]["text"] == "vender mas rapido cada"
    assert [p["start"] for p in out] == sorted(p["start"] for p in out)
    for p in out:
        assert p["end"] > p["start"]


def test_fallback_empty_without_cues(tmp_path):
    assert hp._fallback_phrases(_ctx(tmp_path, {"words": WORDS}), WORDS) == []
