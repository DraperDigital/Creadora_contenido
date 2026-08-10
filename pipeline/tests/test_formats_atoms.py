import json

from contenido_bionico.short.formats import atoms


def test_caption_cues_reads_cues(tmp_path):
    (tmp_path / "captions_props.json").write_text(
        json.dumps({"cues": [{"text": "Hola", "start": 0.0, "end": 1.0}]}), encoding="utf-8"
    )
    cues = atoms.caption_cues(tmp_path)
    assert cues == [{"text": "Hola", "start": 0.0, "end": 1.0}]


def test_caption_cues_missing_returns_none(tmp_path):
    assert atoms.caption_cues(tmp_path) is None


def test_load_carousel_plan(tmp_path):
    (tmp_path / "carousel").mkdir()
    (tmp_path / "carousel" / "Carousel_Plan.json").write_text(
        json.dumps({"title": "T", "slides": [{"kind": "hero"}]}), encoding="utf-8"
    )
    plan = atoms.load_carousel_plan(tmp_path)
    assert plan["title"] == "T"
    assert plan["slides"][0]["kind"] == "hero"


def test_load_carousel_plan_missing(tmp_path):
    assert atoms.load_carousel_plan(tmp_path) is None


def test_load_quote_plan(tmp_path):
    (tmp_path / "quotes").mkdir()
    (tmp_path / "quotes" / "Quote_Plan.json").write_text(
        json.dumps({"quotes": [{"text": "Q", "fontPx": 90, "music": "m.mp3"}]}), encoding="utf-8"
    )
    plan = atoms.load_quote_plan(tmp_path)
    assert plan["quotes"][0]["text"] == "Q"


def test_load_quote_plan_missing(tmp_path):
    assert atoms.load_quote_plan(tmp_path) is None


def test_palette_from_style_tokens(tmp_path):
    (tmp_path / "Style_Tokens.json").write_text(
        json.dumps({"palette": {"bg": "#000000", "ink": "#111111", "paper": "#ffffff",
                                "accent": "#ff0000", "accentSoft": "#ff8888"}}),
        encoding="utf-8",
    )
    pal = atoms.palette(tmp_path)
    assert pal["bg"] == "#000000"
    assert pal["accent"] == "#ff0000"


def test_palette_defaults_when_missing(tmp_path):
    pal = atoms.palette(tmp_path)
    for key in ("bg", "ink", "paper", "accent", "accentSoft"):
        assert key in pal and isinstance(pal[key], str) and pal[key]


def test_palette_fills_missing_keys(tmp_path):
    (tmp_path / "Style_Tokens.json").write_text(
        json.dumps({"palette": {"accent": "#abcdef"}}), encoding="utf-8"
    )
    pal = atoms.palette(tmp_path)
    assert pal["accent"] == "#abcdef"
    for key in ("bg", "ink", "paper", "accentSoft"):
        assert key in pal and pal[key]


def test_voice_track_prefers_the_clean_cut(tmp_path, monkeypatch):
    # Everything voice-related must stem from the clean cut (source.mp4) — the
    # exact timeline the transcript is aligned to — not a downstream assembled
    # artifact. So source.mp4 wins even when final.video_only.mp4 exists.
    encoded = {}

    def fake_encode(src, out):
        encoded["src"] = src
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"audio")
        return True

    monkeypatch.setattr(atoms, "_encode_voice", fake_encode)
    (tmp_path / "source.mp4").write_bytes(b"s")
    (tmp_path / "final.mp4").write_bytes(b"f")
    (tmp_path / "final.video_only.mp4").write_bytes(b"vo")
    out = atoms.voice_track(tmp_path)
    assert out == tmp_path / "_intermediates" / "voice_norm.m4a"
    assert encoded["src"] == tmp_path / "source.mp4"


def test_voice_track_falls_back_to_video_only_when_no_source(tmp_path, monkeypatch):
    encoded = {}
    monkeypatch.setattr(atoms, "_encode_voice",
                        lambda src, out: (encoded.__setitem__("src", src), out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"a"), True)[-1])
    (tmp_path / "final.video_only.mp4").write_bytes(b"vo")
    atoms.voice_track(tmp_path)
    assert encoded["src"] == tmp_path / "final.video_only.mp4"


def test_voice_track_never_uses_the_mixed_reel(tmp_path, monkeypatch):
    # final.mp4 is the fully-mixed reel (voice + music + SFX); extracting "voice"
    # from it would bake music/SFX into every voice-based format. It must NOT be
    # a voice source — with only final.mp4 present, there is no clean voice.
    monkeypatch.setattr(atoms, "_encode_voice", lambda src, out: True)
    (tmp_path / "final.mp4").write_bytes(b"mixed")
    assert atoms.voice_track(tmp_path) is None


def test_voice_track_none_without_source(tmp_path, monkeypatch):
    monkeypatch.setattr(atoms, "_encode_voice", lambda src, out: True)
    assert atoms.voice_track(tmp_path) is None


def test_voice_track_cached(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_encode(src, out):
        calls["n"] += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"audio")
        return True

    monkeypatch.setattr(atoms, "_encode_voice", fake_encode)
    (tmp_path / "source.mp4").write_bytes(b"s")
    first = atoms.voice_track(tmp_path)
    second = atoms.voice_track(tmp_path)
    assert first == second
    assert calls["n"] == 1  # second call reused the cache
