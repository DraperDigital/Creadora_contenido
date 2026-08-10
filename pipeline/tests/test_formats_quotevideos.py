"""WS6 quote-video producers: produce_quote_foto + produce_quote_karaoke.

Pure-python behavior (span slicing/re-zeroing, skip paths, out_dir reuse, and
command/prop construction) is exercised with the heavy edges monkeypatched:
the Remotion renders, `make_quote_video`, `extract_audio_span`, and the mux
`subprocess.run`. `find_verbatim_span` runs for real (deterministic). Also
pinned: the karaoke mux's optional ducked Audio_Plan music bed (command +
filter construction) and the adapted-vs-raw text split — quote_foto's own
stills use `frase_adapt.adapted_citas` while karaoke always keeps the raw
quote-plan texts its voice sync depends on.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import quote_videos

# A short verbatim quote whose words appear contiguously in TRANSCRIPT below.
QUOTE = "vender mas rapido"
_W = lambda t, s, e: {"text": t, "start": s, "end": e, "type": "word"}
TRANSCRIPT_WORDS = [
    _W("Hoy", 0.0, 0.3), _W("te", 0.3, 0.4), _W("explico", 0.4, 0.9),
    _W("como", 1.0, 1.2), _W("vender", 1.2, 1.6), _W("mas", 1.6, 2.0),
    _W("rapido", 2.1, 2.6), _W("hoy", 2.6, 2.9),
]


class FakeCtx:
    """Minimal FormatContext surface the producers touch."""

    def __init__(self, tmp_path, *, quotes, words=None, voice=None, palette=None):
        self.video_id = "9106_short"
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = tmp_path / "out"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._quotes = {"quotes": list(quotes)}
        self._words = words
        self._voice = voice
        self._palette = palette or {
            "bg": "#101010", "ink": "#202020", "paper": "#eeeeee",
            "accent": "#ff5500", "accentSoft": "#ffaa88",
        }

    def quote_plan(self):
        return self._quotes

    def transcript(self):
        return {"words": self._words} if self._words is not None else None

    def voice(self):
        return self._voice

    def palette(self):
        return self._palette


@pytest.fixture
def patch_render(monkeypatch):
    """Capture render/extract/mux calls; create the files they'd produce."""
    calls = SimpleNamespace(video=[], stills=[], extract=[], mux=[])

    def fake_video(**kw):
        kw["out_mp4"].parent.mkdir(parents=True, exist_ok=True)
        kw["out_mp4"].write_bytes(b"v")
        calls.video.append(kw)
        return kw["out_mp4"]

    def fake_stills(**kw):
        for p in kw["out_pngs"]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"p")
        calls.stills.append(kw)
        return kw["out_pngs"]

    def fake_extract(voice, start, end, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"a")
        calls.extract.append((voice, start, end, out))
        return out

    def fake_run(cmd, **kw):
        calls.mux.append((cmd, kw))
        # The mux writes its output (last argv token) so the path is real.
        Path(cmd[-1]).write_bytes(b"m")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(quote_videos, "render_format_video", fake_video)
    monkeypatch.setattr(quote_videos, "render_format_stills", fake_stills)
    monkeypatch.setattr(quote_videos, "extract_audio_span", fake_extract)
    monkeypatch.setattr(quote_videos.subprocess, "run", fake_run)
    return calls


# --------------------------------------------------------------------------- #
# produce_quote_karaoke
# --------------------------------------------------------------------------- #

def test_karaoke_slices_rezeros_and_muxes(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[{"text": QUOTE}], words=TRANSCRIPT_WORDS,
                  voice=tmp_path / "voice.m4a")
    out = quote_videos.produce_quote_karaoke(ctx)

    assert [p.name for p in out] == ["quote_karaoke_1.mp4"]
    assert out[0] == ctx.out_dir / "quote_karaoke_1.mp4"
    assert out[0].exists()

    # The audio span is the verbatim match [1.2, 2.6].
    voice, start, end, _ = patch_render.extract[0]
    assert (round(start, 3), round(end, 3)) == (1.2, 2.6)

    # The comp receives span-relative, re-zeroed words + durationSec = span+0.8.
    props = patch_render.video[0]["props"]
    words = props["words"]
    assert [w["word"] for w in words] == ["vender", "mas", "rapido"]
    assert round(words[0]["start"], 3) == 0.0          # first word re-zeroed
    assert round(words[-1]["end"], 3) == round(2.6 - 1.2, 3)  # 1.4
    assert round(props["durationSec"], 3) == round((2.6 - 1.2) + 0.8, 3)  # 2.2
    assert props["text"] == QUOTE
    assert props["palette"] == ctx.palette()
    assert patch_render.video[0]["component_file"] == "formats/KaraokeQuote"

    # duration_frames tracks durationSec at 30fps.
    assert patch_render.video[0]["duration_frames"] == round(2.2 * 30)

    # Mux: -map 0:v / -map 1:a, copy video, aac audio, NO -shortest (tail dwell).
    cmd, kw = patch_render.mux[0]
    assert cmd[0] == "ffmpeg"
    joined = " ".join(str(c) for c in cmd)
    assert "-map 0:v" in joined and "-map 1:a" in joined
    assert "-c:v copy" in joined
    assert "-c:a aac" in joined
    assert "-shortest" not in cmd
    assert kw.get("check") is True


def test_karaoke_skips_nonmatching_quote(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[{"text": "frase que no aparece jamas"}],
                  words=TRANSCRIPT_WORDS, voice=tmp_path / "voice.m4a")
    assert quote_videos.produce_quote_karaoke(ctx) == []
    assert patch_render.video == []


def test_karaoke_adds_ducked_music_bed_from_plan(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[{"text": QUOTE}], words=TRANSCRIPT_WORDS,
                  voice=tmp_path / "voice.m4a")
    music_file = tmp_path / "bed.mp3"
    music_file.write_bytes(b"m")
    (ctx.run_dir / "Audio_Plan.json").write_text(
        json.dumps({"music": {"file": str(music_file), "base_gain_db": -2.5}}),
        encoding="utf-8",
    )
    out = quote_videos.produce_quote_karaoke(ctx)
    assert [p.name for p in out] == ["quote_karaoke_1.mp4"]
    cmd, _kw = patch_render.mux[0]
    joined = " ".join(str(c) for c in cmd)
    assert "-stream_loop -1" in joined and str(music_file) in cmd
    assert "-map 0:v" in joined and "-map [aout]" in joined
    assert "-map 1:a" not in joined                    # the bed graph owns the audio
    assert "-shortest" not in cmd                      # tail dwell preserved
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert quote_videos._DUCK_FILTER in fc             # bed ducks under the voice
    assert "volume=-2.5dB" in fc                       # plan gain applied
    assert "atrim=duration=2.200" in fc                # span 1.4 + 0.8 tail = video length
    assert "alimiter=limit=0.891" in fc                # treatments' master discipline


def test_karaoke_without_music_plan_keeps_plain_mux(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[{"text": QUOTE}], words=TRANSCRIPT_WORDS,
                  voice=tmp_path / "voice.m4a")
    quote_videos.produce_quote_karaoke(ctx)
    cmd, _kw = patch_render.mux[0]
    joined = " ".join(str(c) for c in cmd)
    assert "-map 1:a" in joined and "-filter_complex" not in cmd
    assert "-stream_loop" not in cmd


def test_karaoke_music_filter_shape():
    fc = quote_videos._karaoke_music_filter(2.2, -1.0)
    assert fc.count("[aout]") == 1
    assert "asplit=2[kq_voice][kq_vsc0]" in fc
    assert "apad=pad_dur=1.800" in fc                  # sidechain outlives the voice span
    assert "atrim=duration=2.200" in fc
    assert "afade=t=in:st=0:d=0.3" in fc
    assert "afade=t=out:st=1.600:d=0.600" in fc        # fade lands on the video end
    assert "volume=-1.0dB" in fc
    assert quote_videos._DUCK_FILTER in fc
    assert "amix=inputs=2:normalize=0:dropout_transition=0" in fc
    assert "alimiter=limit=0.891" in fc


def test_karaoke_keeps_raw_quote_text_never_adapted(tmp_path, patch_render, monkeypatch):
    def boom(ctx):  # noqa: ARG001
        raise AssertionError("karaoke must not consult frase_adapt")

    monkeypatch.setattr(quote_videos.frase_adapt, "adapted_citas", boom)
    ctx = FakeCtx(tmp_path, quotes=[{"text": QUOTE}], words=TRANSCRIPT_WORDS,
                  voice=tmp_path / "voice.m4a")
    out = quote_videos.produce_quote_karaoke(ctx)
    assert [p.name for p in out] == ["quote_karaoke_1.mp4"]
    props = patch_render.video[0]["props"]
    assert props["text"] == QUOTE                      # raw, verbatim-matchable text
    assert [w["word"] for w in props["words"]] == ["vender", "mas", "rapido"]


def test_karaoke_caps_at_three(tmp_path, patch_render):
    ctx = FakeCtx(
        tmp_path,
        quotes=[{"text": QUOTE}, {"text": "explico como"}, {"text": "vender mas"},
                {"text": "hoy te explico"}],
        words=TRANSCRIPT_WORDS, voice=tmp_path / "voice.m4a",
    )
    out = quote_videos.produce_quote_karaoke(ctx)
    assert [p.name for p in out] == [
        "quote_karaoke_1.mp4", "quote_karaoke_2.mp4", "quote_karaoke_3.mp4"
    ]


def test_karaoke_missing_voice_returns_empty(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[{"text": QUOTE}], words=TRANSCRIPT_WORDS, voice=None)
    assert quote_videos.produce_quote_karaoke(ctx) == []


def test_karaoke_missing_transcript_returns_empty(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[{"text": QUOTE}], words=None,
                  voice=tmp_path / "voice.m4a")
    assert quote_videos.produce_quote_karaoke(ctx) == []


def test_karaoke_no_quote_plan_returns_empty(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[], words=TRANSCRIPT_WORDS, voice=tmp_path / "voice.m4a")
    assert quote_videos.produce_quote_karaoke(ctx) == []


# --------------------------------------------------------------------------- #
# produce_quote_foto
# --------------------------------------------------------------------------- #

@pytest.fixture
def patch_foto(monkeypatch):
    """Fake photos, music, and make_quote_video for the quote_foto path."""
    calls = SimpleNamespace(make=[])

    def fake_make(png, music, out_mp4):
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        out_mp4.write_bytes(b"mp4")
        calls.make.append((Path(png), Path(music), Path(out_mp4)))

    monkeypatch.setattr(quote_videos.build_quotes, "make_quote_video", fake_make)
    monkeypatch.setattr(quote_videos.build_quotes, "_music_tracks",
                        lambda: [Path("track.mp3")])
    return calls


def _photos(monkeypatch, tmp_path, n):
    photo_dir = tmp_path / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    photos = []
    for i in range(n):
        p = photo_dir / f"photo_{i}.jpg"
        p.write_bytes(b"jpeg-bytes-%d" % i)
        photos.append(p)
    monkeypatch.setattr(quote_videos.broll, "select_photos",
                        lambda ctx, k, purpose: photos[:k])
    return photos


def test_quote_foto_reuses_existing_still(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 3)
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Una frase fuerte"}])
    (ctx.out_dir / "cita_foto_1.png").write_bytes(b"existing")

    out = quote_videos.produce_quote_foto(ctx)
    assert [p.name for p in out] == ["quote_foto_1.mp4"]
    # Reused the existing still; did NOT render FraseImagen.
    assert patch_render.stills == []
    used_png, _, out_mp4 = patch_foto.make[0]
    assert used_png == ctx.out_dir / "cita_foto_1.png"
    assert out_mp4 == ctx.out_dir / "quote_foto_1.mp4"


def test_quote_foto_renders_still_when_absent(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 1)
    # Keep the render off the shared/ tree: fake the staging target.
    monkeypatch.setattr(quote_videos, "REMOTION_PUBLIC", tmp_path / "public")
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Frase sin still previa"}])

    out = quote_videos.produce_quote_foto(ctx)
    assert [p.name for p in out] == ["quote_foto_1.mp4"]
    # Rendered exactly one FraseImagen still, style "foto".
    assert len(patch_render.stills) == 1
    kw = patch_render.stills[0]
    assert kw["component_file"] == "formats/FraseImagen"
    assert kw["props"]["style"] == "foto"
    assert kw["props"]["text"] == "Frase sin still previa"
    assert kw["width"] == 1080 and kw["height"] == 1350
    # The rendered still (not a cita_foto_*) fed make_quote_video.
    used_png, _, _ = patch_foto.make[0]
    assert used_png.exists()


def test_quote_foto_renders_still_with_adapted_text(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 1)
    monkeypatch.setattr(quote_videos, "REMOTION_PUBLIC", tmp_path / "public")
    monkeypatch.setattr(quote_videos.frase_adapt, "adapted_citas",
                        lambda ctx: ["Version adaptada corta"])
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Frase original larga"}])
    out = quote_videos.produce_quote_foto(ctx)
    assert [p.name for p in out] == ["quote_foto_1.mp4"]
    # Its OWN still renders the adapted text (same cached call cita_foto uses).
    assert patch_render.stills[0]["props"]["text"] == "Version adaptada corta"


def test_quote_foto_reused_still_skips_adaptation(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 3)
    called = {"n": 0}
    monkeypatch.setattr(quote_videos.frase_adapt, "adapted_citas",
                        lambda ctx: called.__setitem__("n", called["n"] + 1) or [])
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Una frase fuerte"}])
    (ctx.out_dir / "cita_foto_1.png").write_bytes(b"existing")
    out = quote_videos.produce_quote_foto(ctx)
    assert [p.name for p in out] == ["quote_foto_1.mp4"]
    assert called["n"] == 0                            # reuse path never adapts
    assert patch_render.stills == []


def test_quote_foto_falls_back_to_raw_when_adaptation_short(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(quote_videos, "REMOTION_PUBLIC", tmp_path / "public")
    monkeypatch.setattr(quote_videos.frase_adapt, "adapted_citas", lambda ctx: ["Solo una"])
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Q1"}, {"text": "Q2"}])
    quote_videos.produce_quote_foto(ctx)
    texts = [kw["props"]["text"] for kw in patch_render.stills]
    assert texts == ["Solo una", "Q2"]                 # index-paired; shortfall -> raw


def test_quote_foto_skips_without_photos(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 0)
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Frase"}])
    assert quote_videos.produce_quote_foto(ctx) == []
    assert patch_foto.make == []


def test_quote_foto_skips_without_music(tmp_path, patch_render, monkeypatch):
    _photos(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(quote_videos.build_quotes, "_music_tracks", lambda: [])
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Frase"}])
    assert quote_videos.produce_quote_foto(ctx) == []


def test_quote_foto_pairs_top3_quotes_with_photos(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 3)
    monkeypatch.setattr(quote_videos, "REMOTION_PUBLIC", tmp_path / "public")
    ctx = FakeCtx(tmp_path, quotes=[{"text": "Q1"}, {"text": "Q2"}, {"text": "Q3"}, {"text": "Q4"}])
    out = quote_videos.produce_quote_foto(ctx)
    assert [p.name for p in out] == [
        "quote_foto_1.mp4", "quote_foto_2.mp4", "quote_foto_3.mp4"
    ]


def test_quote_foto_no_quote_plan_returns_empty(tmp_path, patch_render, patch_foto, monkeypatch):
    _photos(monkeypatch, tmp_path, 3)
    ctx = FakeCtx(tmp_path, quotes=[])
    assert quote_videos.produce_quote_foto(ctx) == []


# --------------------------------------------------------------------------- #
# produce_quote_tecleado (typed quote + highlighter)
# --------------------------------------------------------------------------- #

def test_produce_quote_tecleado_types_and_highlights(tmp_path, patch_render, monkeypatch):
    monkeypatch.setattr(quote_videos.frase_adapt, "adapted_citas", lambda ctx: [])
    monkeypatch.setattr(quote_videos._vc, "_pick_music", lambda ctx: tmp_path / "bed.mp3")
    monkeypatch.setattr(quote_videos._vc, "_build_teclado_audio",
                        lambda schedule, seconds, work, music=None: work / "a.m4a")
    monkeypatch.setattr(quote_videos._vc, "_mux_audio", lambda v, a, out: out)
    ctx = FakeCtx(tmp_path, quotes=[
        {"text": "Nadie sabe que funciona en redes sociales"},
        {"text": "Publica y el algoritmo te responde rapido"},
    ])
    out = quote_videos.produce_quote_tecleado(ctx)
    assert [p.name for p in out] == ["quote_tecleado_1.mp4", "quote_tecleado_2.mp4"]
    vids = patch_render.video
    assert all(kw["component_file"] == "formats/QuoteTecleado" for kw in vids)
    props0 = vids[0]["props"]
    assert props0["text"].startswith("Nadie")
    # highlighter marks 1-2 real content words (>= 4 chars, no function words).
    assert 1 <= len(props0["highlights"]) <= 2
    assert all(len(h) >= 4 for h in props0["highlights"])
    # types in over part of the clip, then holds.
    assert 0 < props0["typeEndSec"] < quote_videos._TECLADO_CLIP_SEC


def test_produce_quote_tecleado_no_quotes_skips(tmp_path, patch_render):
    ctx = FakeCtx(tmp_path, quotes=[])
    assert quote_videos.produce_quote_tecleado(ctx) == []
