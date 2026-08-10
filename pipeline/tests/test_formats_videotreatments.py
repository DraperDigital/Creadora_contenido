"""Deterministic tests for the video-treatment builders + producer wiring.

ffmpeg and Remotion never run here: `_run_ffmpeg`, `_probe_duration`,
`render_format_overlay` and `hero_phrases` are all monkeypatched. The tests pin
the pure math (hex conversion, music-plan reading), the audio/video graph shape
(ducking vs voice-only, camera chain on subtitulado, overlay/vstack structure),
producer behavior (atom gating, correct inputs + output path, cutaway mode for
herotext), and the cutaway phrase-selection constraints (word count, dwell
clamps, impact-order ≥8s spacing, caps, fallback) in `hero_phrases`.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import hero_phrases as hp
from contenido_bionico.short.formats import video_treatments as vt


# --------------------------------------------------------------- pure helpers -

def test_hex_to_ff():
    assert vt._hex_to_ff("#1A1712") == "0x1A1712"
    assert vt._hex_to_ff("1a1712") == "0x1A1712"
    assert vt._hex_to_ff("black") == "black"


def test_music_from_plan_present(tmp_path):
    music_file = tmp_path / "song.mp3"
    music_file.write_bytes(b"x")
    (tmp_path / "Audio_Plan.json").write_text(
        json.dumps({"music": {"file": str(music_file), "base_gain_db": -1.95}}),
        encoding="utf-8",
    )
    assert vt._music_from_plan(tmp_path) == (str(music_file), -1.95)


def test_music_from_plan_missing_file(tmp_path):
    (tmp_path / "Audio_Plan.json").write_text(
        json.dumps({"music": {"file": str(tmp_path / "gone.mp3"), "base_gain_db": -2.0}}),
        encoding="utf-8",
    )
    assert vt._music_from_plan(tmp_path) is None


def test_music_from_plan_no_music(tmp_path):
    (tmp_path / "Audio_Plan.json").write_text(json.dumps({"music": None}), encoding="utf-8")
    assert vt._music_from_plan(tmp_path) is None
    assert vt._music_from_plan(tmp_path) is None  # missing file too


# ------------------------------------------------------------- audio graphs ---

def test_voice_only_audio_has_limiter_no_duck():
    lines = vt._voice_music_audio(2, None, 0.0, 20.0)
    graph = "\n".join(lines)
    assert "alimiter=limit=0.891" in graph
    assert "sidechaincompress" not in graph
    assert graph.endswith("[aout]")


def test_voice_music_audio_ducks_and_masters():
    lines = vt._voice_music_audio(2, 3, -1.95, 20.0)
    graph = "\n".join(lines)
    assert "asplit=2[hv_voice][hv_vsc]" in graph
    assert vt._DUCK_FILTER in graph
    assert "volume=-1.95dB" in graph          # music bed at its plan gain
    assert "atrim=duration=20.000" in graph
    assert "alimiter=limit=0.891" in graph
    assert graph.strip().endswith("[aout]")


# ------------------------------------------------------------- video graphs ---

def test_clip_subtitulado_graph_overlays_captions():
    g = vt._build_clip_subtitulado_graph(has_music=False, music_gain_db=0.0, duration=20.0)
    assert "[0:v]scale=1080:1920" in g              # opaque base (no camera chain)
    assert "zoompan=" not in g
    assert "[base][caps]overlay=0:0:format=auto:eof_action=pass:repeatlast=0[vout]" in g
    assert "sidechaincompress" not in g             # no music -> no duck


def test_clip_subtitulado_graph_uses_camera_chain_when_given():
    chain = "[0:v]scale=3240:5760,zoompan=z='1.0':d=1[base]"
    g = vt._build_clip_subtitulado_graph(
        camera_chain=chain, has_music=False, music_gain_db=0.0, duration=20.0
    )
    assert g.startswith(chain)                      # camera replaces the static base
    assert "[0:v]scale=1080:1920" not in g          # no plain scale/pad base
    assert "[base][caps]overlay=0:0:format=auto:eof_action=pass:repeatlast=0[vout]" in g


def test_splitscreen_graph_stacks_over_color_band():
    g = vt._build_splitscreen_graph(
        top_chain="[0:v]null[toph]", bg_ff="0x1A1712",
        has_music=False, music_gain_db=0.0, duration=20.0,
    )
    assert "color=c=0x1A1712:s=1080x960" in g
    assert "[toph]format=yuv420p[topf]" in g            # pinned before stacking
    assert "[topf][bottom]vstack=inputs=2,setsar=1[vout]" in g


# ------------------------------------------------------------- producer wiring -

def _ctx(tmp_path, **atoms):
    run_dir = tmp_path / "run"
    out_dir = tmp_path / "out"
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "source": run_dir / "source.mp4",
        "captions_webm": run_dir / "captions.webm",
        "voice": run_dir / "_intermediates" / "voice_norm.m4a",
        "transcript": {"words": [
            {"text": "hola", "start": 0.0, "end": 0.5, "type": "word"},
            {"text": "mundo", "start": 0.6, "end": 1.2, "type": "word"},
        ]},
        "palette": {"bg": "#1A1712", "ink": "#231D17", "paper": "#F6F1E7",
                    "accent": "#E8724C", "accentSoft": "#F4B69E"},
    }
    defaults.update(atoms)
    for key in ("source", "captions_webm", "voice"):
        p = defaults[key]
        if isinstance(p, type(run_dir)):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
    return SimpleNamespace(
        video_id="9_short", run_dir=run_dir, out_dir=out_dir,
        source=lambda: defaults["source"],
        captions_webm=lambda: defaults["captions_webm"],
        voice=lambda: defaults["voice"],
        transcript=lambda: defaults["transcript"],
        palette=lambda: defaults["palette"],
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture the ffmpeg argv and materialize the output so _encode passes."""
    calls = []

    def fake_run(args, cwd):
        calls.append({"args": list(args), "cwd": cwd})
        from pathlib import Path
        Path(args[-1]).write_bytes(b"video")

    monkeypatch.setattr(vt, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(vt, "_probe_duration", lambda p: 20.0)
    return calls


def _last_filter_graph(ctx, stem):
    return (ctx.run_dir / "logs" / "formats" / f"{stem}.filter.txt").read_text(encoding="utf-8")


def test_clip_subtitulado_runs(tmp_path, captured):
    ctx = _ctx(tmp_path)
    out = vt.produce_clip_subtitulado(ctx)
    assert out == [ctx.out_dir / "clip_subtitulado.mp4"]
    assert out[0].exists()
    args = captured[0]["args"]
    assert "-c:v" in args and "libvpx-vp9" in args  # captions decoded as vp9
    assert str(ctx.source()) in args
    assert "-stream_loop" not in args               # no music file -> no bed
    graph = _last_filter_graph(ctx, "clip_subtitulado")
    assert "[vout]" in graph and "[aout]" in graph
    assert "zoompan=" in graph                      # camera chain (transcript present)
    assert "[base][caps]overlay=" in graph          # captions still composited on top


def test_clip_subtitulado_no_transcript_falls_back_to_static_base(tmp_path, captured):
    ctx = _ctx(tmp_path, transcript=None)
    out = vt.produce_clip_subtitulado(ctx)
    assert out == [ctx.out_dir / "clip_subtitulado.mp4"]   # still produces
    graph = _last_filter_graph(ctx, "clip_subtitulado")
    assert "zoompan=" not in graph                  # graceful no-camera fallback
    assert "[0:v]scale=1080:1920" in graph          # the classic static base
    assert "[base][caps]overlay=" in graph


def test_clip_subtitulado_adds_music_bed(tmp_path, captured):
    ctx = _ctx(tmp_path)
    music_file = tmp_path / "song.mp3"
    music_file.write_bytes(b"m")
    (ctx.run_dir / "Audio_Plan.json").write_text(
        json.dumps({"music": {"file": str(music_file), "base_gain_db": -1.95}}),
        encoding="utf-8",
    )
    vt.produce_clip_subtitulado(ctx)
    args = captured[0]["args"]
    assert "-stream_loop" in args and str(music_file) in args
    assert "sidechaincompress" in _last_filter_graph(ctx, "clip_subtitulado")


def test_clip_subtitulado_skips_missing_atom(tmp_path, captured):
    ctx = _ctx(tmp_path, captions_webm=None)
    assert vt.produce_clip_subtitulado(ctx) == []
    assert captured == []


def test_herotext_renders_full_overlay_in_cutaway_mode(tmp_path, captured, monkeypatch):
    ctx = _ctx(tmp_path)
    overlay_calls = []
    phrase_calls = []

    def fake_overlay(**kw):
        overlay_calls.append(kw)
        kw["out_webm"].parent.mkdir(parents=True, exist_ok=True)
        kw["out_webm"].write_bytes(b"webm")
        return kw["out_webm"]

    def fake_phrases(ctx, **kw):
        phrase_calls.append(kw)
        return [{"text": "hola mundo", "start": 0.0, "end": 1.2}]

    monkeypatch.setattr(vt, "render_format_overlay", fake_overlay)
    monkeypatch.setattr(vt, "hero_phrases", fake_phrases)
    out = vt.produce_video_herotext(ctx)
    assert out == [ctx.out_dir / "video_herotext.mp4"]
    assert phrase_calls == [{"mode": "cutaway"}]     # herotext selects cutaways
    assert overlay_calls[0]["props"]["zone"] == "full"
    assert overlay_calls[0]["height"] == 1920
    graph = _last_filter_graph(ctx, "video_herotext")
    assert "[base][hero]overlay=" in graph          # camera base + hero overlay
    assert "zoompan=" in graph                       # camera chain present


def test_splitscreen_renders_bottom_overlay(tmp_path, captured, monkeypatch):
    ctx = _ctx(tmp_path)
    overlay_calls = []
    phrase_calls = []

    def fake_overlay(**kw):
        overlay_calls.append(kw)
        kw["out_webm"].parent.mkdir(parents=True, exist_ok=True)
        kw["out_webm"].write_bytes(b"webm")
        return kw["out_webm"]

    def fake_phrases(ctx, **kw):
        phrase_calls.append(kw)
        return []

    monkeypatch.setattr(vt, "render_format_overlay", fake_overlay)
    monkeypatch.setattr(vt, "hero_phrases", fake_phrases)
    # No authored panels and no full-frame scenes -> the hero-text band fallback.
    monkeypatch.setattr(vt, "_author_split_bottoms", lambda vid, duration: None)
    out = vt.produce_video_splitscreen(ctx)
    assert out == [ctx.out_dir / "video_splitscreen.mp4"]
    assert phrase_calls == [{}]                      # splitscreen keeps dense default
    assert overlay_calls[0]["props"]["zone"] == "bottom"
    assert overlay_calls[0]["height"] == 960
    graph = _last_filter_graph(ctx, "video_splitscreen")
    assert "vstack=inputs=2" in graph
    assert "color=c=0x1A1712:s=1080x960" in graph


def test_splitscreen_prefers_authored_bottom_panels(tmp_path, captured, monkeypatch):
    """When the split author produces 1080x960 panels, they are the bottom band:
    looped as an h264 input (no vp9 decoder hint), run through the anim branch
    (scale/crop to the band, not the color-band fallback), and NO hero overlay is
    rendered."""
    ctx = _ctx(tmp_path)
    authored = ctx.run_dir / "_intermediates" / "splitscreen_bottom_authored.mp4"
    authored.parent.mkdir(parents=True, exist_ok=True)
    authored.write_bytes(b"mp4")
    overlay_calls: list = []
    monkeypatch.setattr(vt, "render_format_overlay",
                        lambda **kw: overlay_calls.append(kw) or kw["out_webm"])
    monkeypatch.setattr(vt, "hero_phrases", lambda ctx, **kw: [])
    monkeypatch.setattr(vt, "_author_split_bottoms", lambda vid, duration: authored)

    out = vt.produce_video_splitscreen(ctx)
    assert out == [ctx.out_dir / "video_splitscreen.mp4"]
    assert overlay_calls == []                        # authored panels -> no hero band
    args = captured[-1]["args"]
    assert str(authored) in args
    i = args.index(str(authored))
    assert args[i - 1] == "-i"                         # looped input 1
    assert "libvpx-vp9" not in args[max(0, i - 3):i]   # h264 mp4, no vp9 decoder hint
    graph = _last_filter_graph(ctx, "video_splitscreen")
    assert "vstack=inputs=2" in graph
    assert "crop=1080:960" in graph                    # anim branch (scale/crop to band)
    assert "color=c=0x1A1712:s=1080x960" not in graph  # not the color-band fallback


# ---------------------------------------------------- cutaway phrase selection -
# Pure-unit constraints for hero_phrases(mode="cutaway"): word-count window,
# dwell clamps, impact-order ≥8s spacing, cap, and the spaced fallback.

def _w(text, start, end):
    return {"text": text, "start": start, "end": end, "type": "word"}


def _words_for(*phrases):
    """Word rows for phrases given as (text, start, end): words evenly spread."""
    rows = []
    for text, start, end in phrases:
        toks = text.split()
        step = (end - start) / len(toks)
        for i, tok in enumerate(toks):
            rows.append(_w(tok, round(start + i * step, 3), round(start + (i + 1) * step, 3)))
    return rows


def test_cutaway_accepts_long_statements_dense_does_not():
    words = _words_for(("este es el error que todos cometen", 1.0, 3.0))
    phrases = [{"text": "este es el error que todos cometen", "start": 0, "end": 0}]  # 7 words
    assert hp._validate_and_anchor(phrases, words, mode="dense") == []
    out = hp._validate_and_anchor(phrases, words, mode="cutaway")
    assert [p["text"] for p in out] == ["este es el error que todos cometen"]


def test_cutaway_drops_over_ten_words():
    text = "una frase demasiado larga que no puede ser una tarjeta valida"  # 11 words
    words = _words_for((text, 1.0, 5.0))
    assert hp._validate_and_anchor([{"text": text, "start": 0, "end": 0}], words, mode="cutaway") == []


def test_cutaway_dwell_clamped_up_to_minimum():
    words = _words_for(("empieza hoy", 10.0, 10.5))    # 0.5s spoken span
    out = hp._validate_and_anchor(
        [{"text": "empieza hoy", "start": 0, "end": 0}], words, mode="cutaway"
    )
    assert out == [{"text": "empieza hoy", "start": 10.0, "end": 10.0 + hp.CUTAWAY_DWELL_MIN_S}]


def test_cutaway_dwell_clamped_down_to_maximum():
    text = "la unica cosa que de verdad importa aqui hoy"   # 9 words over 6s
    words = _words_for((text, 20.0, 26.0))
    out = hp._validate_and_anchor([{"text": text, "start": 0, "end": 0}], words, mode="cutaway")
    assert out == [{"text": text, "start": 20.0, "end": 20.0 + hp.CUTAWAY_DWELL_MAX_S}]


def test_cutaway_spacing_keeps_the_more_impactful_of_a_close_pair():
    # Two candidates <8s apart; the agent listed "nadie te lo dice" FIRST (more
    # impactful) even though it starts LATER — impact order must win.
    words = _words_for(
        ("vender mas rapido", 2.0, 3.4),
        ("nadie te lo dice", 6.0, 7.2),
        ("cambia tu vida", 20.0, 21.0),
    )
    agent = [
        {"text": "nadie te lo dice", "start": 0, "end": 0},
        {"text": "vender mas rapido", "start": 0, "end": 0},
        {"text": "cambia tu vida", "start": 0, "end": 0},
    ]
    out = hp._validate_and_anchor(agent, words, mode="cutaway")
    texts = [p["text"] for p in out]
    assert "vender mas rapido" not in texts            # lost the <8s conflict
    assert texts == ["nadie te lo dice", "cambia tu vida"]
    assert [p["start"] for p in out] == sorted(p["start"] for p in out)
    assert out[1]["start"] - out[0]["start"] >= hp.CUTAWAY_MIN_GAP_S


def test_cutaway_caps_at_six_and_sorts_by_start():
    spans = [(f"frase numero {i}", 1.0 + 9.0 * i, 2.2 + 9.0 * i) for i in range(8)]
    words = _words_for(*spans)
    agent = [{"text": t, "start": 0, "end": 0} for t, _s, _e in reversed(spans)]
    out = hp._validate_and_anchor(agent, words, mode="cutaway")
    assert len(out) == hp.CUTAWAY_MAX_PHRASES
    assert [p["start"] for p in out] == sorted(p["start"] for p in out)
    for a, b in zip(out, out[1:]):
        assert b["start"] - a["start"] >= hp.CUTAWAY_MIN_GAP_S


def test_cutaway_fallback_is_fewer_spaced_and_clamped(tmp_path):
    # Cues every 5s: dense keeps all (2s gap), cutaway keeps every other (8s gap)
    # and clamps each dwell to at least 1.2s.
    cues = [
        {"text": f"gancho numero {i} de la lista", "start": 5.0 * i, "end": 5.0 * i + 0.9}
        for i in range(6)
    ]
    (tmp_path / "captions_props.json").write_text(json.dumps({"cues": cues}), encoding="utf-8")
    ctx = SimpleNamespace(run_dir=tmp_path, transcript=lambda: None)
    dense = hp._fallback_phrases(ctx, [], mode="dense")
    cut = hp._fallback_phrases(ctx, [], mode="cutaway")
    assert len(dense) == 6
    assert len(cut) < len(dense)
    for a, b in zip(cut, cut[1:]):
        assert b["start"] - a["start"] >= hp.CUTAWAY_MIN_GAP_S
    for p in cut:
        assert p["end"] - p["start"] >= hp.CUTAWAY_DWELL_MIN_S - 1e-6


def test_hero_phrases_cutaway_falls_back_below_three_survivors(tmp_path, monkeypatch):
    words = _words_for(("vender mas rapido", 2.0, 3.4), ("cambia tu vida", 20.0, 21.0))
    agent = [
        {"text": "vender mas rapido", "start": 0, "end": 0},
        {"text": "cambia tu vida", "start": 0, "end": 0},          # only 2 survive
        {"text": "no existe en absoluto", "start": 0, "end": 0},
    ]
    monkeypatch.setattr(hp, "_select_phrases", lambda ctx, w, *a, **k: agent)
    sentinel = [{"text": "fb", "start": 0.0, "end": 1.2}]
    monkeypatch.setattr(hp, "_fallback_phrases", lambda *a, **k: sentinel)
    ctx = SimpleNamespace(run_dir=tmp_path, transcript=lambda: {"words": words})
    assert hp.hero_phrases(ctx, mode="cutaway") is sentinel


def test_hero_phrases_cutaway_passes_mode_and_keeps_survivors(tmp_path, monkeypatch):
    spans = [(f"frase numero {i}", 1.0 + 9.0 * i, 2.2 + 9.0 * i) for i in range(4)]
    words = _words_for(*spans)
    seen = {}

    def fake_select(ctx, w, *args, **kwargs):
        seen["mode"] = args[0] if args else kwargs.get("mode", "dense")
        return [{"text": t, "start": 0, "end": 0} for t, _s, _e in spans]

    monkeypatch.setattr(hp, "_select_phrases", fake_select)
    ctx = SimpleNamespace(run_dir=tmp_path, transcript=lambda: {"words": words})
    out = hp.hero_phrases(ctx, mode="cutaway")
    assert seen["mode"] == "cutaway"
    assert len(out) == 4
    assert all(p["end"] - p["start"] >= hp.CUTAWAY_DWELL_MIN_S - 1e-6 for p in out)
