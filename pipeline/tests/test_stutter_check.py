"""Unit tests for ``shared/cut/stutter_check.py`` exact-duplicate detection.

The n-gram floor is passed EXPLICITLY in every call so these tests pin the
detection algorithm itself, not the module default (which is a product knob).
"""
from contenido_bionico.shared.cut.stutter_check import find_stutters, format_findings


def _find(text, min_ngram=3, max_ngram=12):
    return find_stutters(text, min_ngram=min_ngram, max_ngram=max_ngram)


def test_detects_back_to_back_duplicate_trigram():
    text = "Y en caso de que, y en caso de que se te olvide"
    findings = _find(text)
    assert len(findings) == 1
    f = findings[0]
    assert f["start_token"] == 0
    # Longest match wins: the whole 5-token phrase, not overlapping 3-grams.
    assert f["ngram_length"] == 5
    assert f["text"].startswith("Y en caso de que,")


def test_clean_text_has_no_findings():
    text = "Hoy vamos a ver tres errores comunes al grabar un video corto"
    assert _find(text) == []
    assert format_findings([]) == ""


def test_normalization_matches_case_accents_and_edge_punctuation():
    # "Qué tal, amigos" vs "que tal amigos" normalize identically.
    text = "Qué tal, amigos. que tal amigos, empezamos ya"
    findings = _find(text)
    assert len(findings) == 1
    assert findings[0]["ngram_length"] == 3
    # `text` keeps the RAW surface form of both occurrences.
    assert findings[0]["text"] == "Qué tal, amigos. que tal amigos,"


def test_non_adjacent_repetition_is_not_a_stutter():
    # The 3-gram repeats but not back-to-back: no finding.
    text = "en caso de duda revisa y en caso de error corrige"
    assert _find(text) == []


def test_min_ngram_floor_is_respected():
    # A 2-token duplicate is below an explicit floor of 3.
    text = "muy bien muy bien sigamos con el tema de hoy"
    assert _find(text, min_ngram=3) == []
    # Lowering the floor to 2 flags it.
    findings = _find(text, min_ngram=2)
    assert len(findings) == 1
    assert findings[0]["ngram_length"] == 2


def test_multiple_stutters_reported_separately():
    text = (
        "esto es lo primero esto es lo primero y luego "
        "viene el final viene el final de verdad"
    )
    findings = _find(text)
    assert [f["ngram_length"] for f in findings] == [4, 3]
    assert findings[0]["start_token"] == 0


def test_format_findings_mentions_every_finding():
    text = "Y en caso de que, y en caso de que se te olvide"
    findings = _find(text)
    rendered = format_findings(findings)
    assert "1." in rendered
    assert "n-grama de 5" in rendered
    assert "Contexto:" in rendered
