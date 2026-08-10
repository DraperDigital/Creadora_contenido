"""Unit tests for the V6.1 adaptation foundation: `formats.adapt` transport
helpers and the cached `formats.frase_adapt` accessors. No ffmpeg, no network —
the single agent seam is monkeypatched."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import adapt, frase_adapt


# --- adapt.parse_json_object ---------------------------------------------------

def test_parse_tolerates_prose_fences_and_trailing_junk():
    stdout = "Sure! Here you go:\n```json\n{\"a\": 1}\n```\nDone."
    assert adapt.parse_json_object(stdout, "x") == {"a": 1}


def test_parse_tolerates_stray_extra_closing_brace():
    assert adapt.parse_json_object('{"a": {"b": 2}}}', "x") == {"a": {"b": 2}}


def test_parse_rejects_no_json_and_non_object():
    with pytest.raises(adapt.AdaptError):
        adapt.parse_json_object("no json here", "x")
    with pytest.raises(adapt.AdaptError):
        adapt.parse_json_object("[1, 2]", "x")


# --- adapt.transcript_text -----------------------------------------------------

def test_transcript_text_joins_word_tokens_and_falls_back():
    t = {
        "words": [
            {"type": "word", "text": "Hola"},
            {"type": "spacing", "text": " "},
            {"type": "word", "text": "mundo"},
        ]
    }
    assert adapt.transcript_text(t) == "Hola mundo"
    assert adapt.transcript_text({"words": [], "text": "  plano  b  "}) == "plano b"
    assert adapt.transcript_text(None) == ""


# --- frase_adapt fixtures ------------------------------------------------------

QUOTES = ["Primera cita del video sobre ventas", "Segunda cita con y para todos"]


def _ctx(tmp_path, quotes=None, transcript_ok=True):
    words = [{"type": "word", "text": w} for w in "hola esto es el transcript".split()]
    return SimpleNamespace(
        run_dir=tmp_path,
        transcript=lambda: ({"words": words} if transcript_ok else None),
        quote_plan=lambda: {"quotes": [{"text": q} for q in (QUOTES if quotes is None else quotes)]},
    )


@pytest.fixture(autouse=True)
def _fresh_memo():
    frase_adapt._reset_memo()
    yield
    frase_adapt._reset_memo()


def _patch_agent(monkeypatch, payload=None, error=False):
    calls = {"n": 0}

    def fake(ctx, raw_quotes):
        calls["n"] += 1
        if error:
            raise adapt.AdaptError("boom")
        return payload

    monkeypatch.setattr(frase_adapt, "_call_agent", fake)
    return calls


# --- frase_adapt: success path, memo, disk cache -------------------------------

def test_adapted_values_from_one_agent_call(tmp_path, monkeypatch):
    calls = _patch_agent(
        monkeypatch,
        {"citas": ["Cita uno fit", "Cita dos fit"], "frase": "La frase poster", "palabra": "VENDE"},
    )
    ctx = _ctx(tmp_path)
    assert frase_adapt.adapted_citas(ctx) == ["Cita uno fit", "Cita dos fit"]
    assert frase_adapt.adapted_frase(ctx) == "La frase poster"
    assert frase_adapt.hero_word(ctx) == "VENDE"
    assert calls["n"] == 1  # memoized across the three accessors

    disk = json.loads((tmp_path / "_intermediates" / frase_adapt.CACHE_NAME).read_text("utf-8"))
    assert disk["citas"] == ["Cita uno fit", "Cita dos fit"]

    # A fresh process (cleared memo) must reuse the disk cache, not the agent.
    frase_adapt._reset_memo()
    assert frase_adapt.adapted_citas(_ctx(tmp_path)) == ["Cita uno fit", "Cita dos fit"]
    assert calls["n"] == 1


def test_changed_quotes_invalidate_disk_cache(tmp_path, monkeypatch):
    calls = _patch_agent(monkeypatch, {"citas": ["A", "B"], "frase": "F", "palabra": "P"})
    frase_adapt.adapted_citas(_ctx(tmp_path))
    frase_adapt._reset_memo()
    frase_adapt.adapted_citas(_ctx(tmp_path, quotes=["otra cita distinta"]))
    assert calls["n"] == 2


# --- frase_adapt: normalization ------------------------------------------------

def test_short_agent_list_pads_with_raw_and_clamps(tmp_path, monkeypatch):
    _patch_agent(
        monkeypatch,
        {"citas": ["Solo una"], "frase": "x" * 500, "palabra": "una frase demasiado larga para portada"},
    )
    ctx = _ctx(tmp_path)
    citas = frase_adapt.adapted_citas(ctx)
    assert citas == ["Solo una", QUOTES[1]]
    assert len(frase_adapt.adapted_frase(ctx)) <= frase_adapt.MAX_FRASE_CHARS
    palabra = frase_adapt.hero_word(ctx)
    assert palabra and len(palabra) <= frase_adapt.MAX_WORD_CHARS


# --- frase_adapt: fallbacks ----------------------------------------------------

def test_agent_failure_falls_back_to_raw_quotes(tmp_path, monkeypatch):
    _patch_agent(monkeypatch, error=True)
    ctx = _ctx(tmp_path)
    assert frase_adapt.adapted_citas(ctx) == QUOTES
    assert frase_adapt.adapted_frase(ctx) == QUOTES[0]
    word = frase_adapt.hero_word(ctx)
    assert word == "PRIMERA"  # longest significant token, stopwords skipped
    assert not (tmp_path / "_intermediates" / frase_adapt.CACHE_NAME).exists()


def test_no_quotes_means_no_agent_call_and_empty_fallbacks(tmp_path, monkeypatch):
    calls = _patch_agent(monkeypatch, {"citas": [], "frase": "F", "palabra": "P"})
    ctx = _ctx(tmp_path, quotes=[])
    assert frase_adapt.adapted_citas(ctx) == []
    assert frase_adapt.adapted_frase(ctx) is None
    assert frase_adapt.hero_word(ctx) is None
    assert calls["n"] == 0


def test_fallback_word_prefers_significant_tokens():
    assert frase_adapt._fallback_word("y de la para con") is not None  # stopword-only still yields something
    assert frase_adapt._fallback_word("no te rindas por el miedo") == "RINDAS"
    assert frase_adapt._fallback_word("") is None


# --- frase_adapt.main_idea (3-7 word big idea) --------------------------------

def test_main_idea_from_agent(tmp_path, monkeypatch):
    _patch_agent(
        monkeypatch,
        {"citas": ["A", "B"], "frase": "F", "palabra": "P", "idea": "Una grabacion cincuenta piezas nuevas"},
    )
    assert frase_adapt.main_idea(_ctx(tmp_path)) == "Una grabacion cincuenta piezas nuevas"


def test_main_idea_clamps_to_seven_words(tmp_path, monkeypatch):
    _patch_agent(
        monkeypatch,
        {"citas": ["A", "B"], "frase": "F", "palabra": "P",
         "idea": "uno dos tres cuatro cinco seis siete ocho nueve"},
    )
    assert len(frase_adapt.main_idea(_ctx(tmp_path)).split()) == frase_adapt.MAX_IDEA_WORDS


def test_main_idea_thin_agent_idea_falls_back_to_frase(tmp_path, monkeypatch):
    # < MIN_IDEA_WORDS -> idea dropped -> first 7 words of the poster phrase.
    _patch_agent(
        monkeypatch,
        {"citas": ["A", "B"], "frase": "Deja de regalar lo que deberias cobrar hoy mismo ya",
         "palabra": "P", "idea": "muy corto"},
    )
    idea = frase_adapt.main_idea(_ctx(tmp_path))
    assert idea == "Deja de regalar lo que deberias cobrar"
    assert 3 <= len(idea.split()) <= frase_adapt.MAX_IDEA_WORDS


def test_main_idea_none_without_quotes(tmp_path, monkeypatch):
    calls = _patch_agent(monkeypatch, {"citas": [], "frase": "F", "palabra": "P", "idea": "x y z"})
    assert frase_adapt.main_idea(_ctx(tmp_path, quotes=[])) is None
    assert calls["n"] == 0
