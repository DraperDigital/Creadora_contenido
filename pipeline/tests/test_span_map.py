from pathlib import Path

from contenido_bionico.short.formats import span_map
from contenido_bionico.short.formats.span_map import find_verbatim_span

W = lambda t, s, e: {"text": t, "start": s, "end": e, "type": "word"}
WORDS = [W("Hoy", 0.0, 0.3), W("te", 0.3, 0.4), W("explico", 0.4, 0.9),
         W("cómo", 1.0, 1.2), W("vender", 1.2, 1.6), W("más,", 1.6, 2.0), W("rápido.", 2.1, 2.6)]


def test_exact_match():
    assert find_verbatim_span("cómo vender más", WORDS) == (1.0, 2.0)


def test_accents_case_punct_insensitive():
    assert find_verbatim_span("COMO VENDER MAS", WORDS) == (1.0, 2.0)


def test_no_match_returns_none():
    assert find_verbatim_span("frase inventada que no existe", WORDS) is None


def test_single_word_match():
    assert find_verbatim_span("explico", WORDS) == (0.4, 0.9)


def test_empty_quote_returns_none():
    assert find_verbatim_span("", WORDS) is None


def test_quote_longer_than_transcript_returns_none():
    assert find_verbatim_span("Hoy te explico cómo vender más rápido de verdad", WORDS) is None


def test_extract_audio_span_command(monkeypatch, tmp_path):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(span_map.subprocess, "run", fake_run)
    out = span_map.extract_audio_span(Path("voice.m4a"), 1.5, 3.25, tmp_path / "span.m4a")
    assert out == tmp_path / "span.m4a"
    cmd = calls["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and "1.500" in cmd
    assert "-to" in cmd and "3.250" in cmd
    assert "-vn" in cmd
    assert calls["kwargs"].get("check") is True


def test_concat_audio_command(monkeypatch, tmp_path):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(span_map.subprocess, "run", fake_run)
    parts = [tmp_path / "a.m4a", tmp_path / "b.m4a", tmp_path / "c.m4a"]
    out = span_map.concat_audio(parts, tmp_path / "joined.m4a", gap_s=0.5)
    assert out == tmp_path / "joined.m4a"
    cmd = calls["cmd"]
    joined = " ".join(cmd)
    assert "concat=n=3:v=0:a=1" in joined
    assert "apad=pad_dur=0.500" in joined  # gap between parts
