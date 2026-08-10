"""WS2: video-carousel producers — deterministic timing + ffmpeg-command TDD.

Renders and real ffmpeg are exercised by the live smoke; here we lock the pure
math (slide extraction, duration/schedule tables, the voz audio/video
invariant, the whatsapp chat schedule), the constructed ffmpeg commands (mux,
amix, music beds, ducked beds) via a monkeypatched subprocess, the adaptation
plumbing (adapted slides count/caps/fallback, conversation validation/cache),
and producer wiring — including the sync rule that `map_slides_to_spans`
always receives the ORIGINAL plan texts while the display uses adapted ones.
"""
import json
from pathlib import Path

import pytest

from contenido_bionico.short.formats import video_carousels as vc
from contenido_bionico.short.formats.adapt import AdaptError

PLAN = {
    "title": "Crea contenido sin editores",
    "slides": [
        {"kind": "hero", "eyebrow": "El sistema", "title": "Crea contenido sin editores", "footnote": "3 claves"},
        {"kind": "item", "index": 1, "total": 3, "heading": "La pereza te frena", "body": "No lo haces por pereza."},
        {"kind": "item", "index": 2, "total": 3, "heading": "Agentes IA", "body": "Un equipo edita el video."},
        {"kind": "item", "index": 3, "total": 3, "heading": "Tu solo grabas", "body": "Grabas y ellos lo montan."},
    ],
}
TRANSCRIPT = {"words": [{"text": f"w{i}", "start": float(i), "end": float(i) + 1.0, "type": "word"} for i in range(30)]}
PALETTE = {"bg": "#111", "ink": "#222", "paper": "#eee", "accent": "#0a0", "accentSoft": "#faa"}

SLIDES_PAYLOAD = {
    "title": "Titulo adaptado",
    "eyebrow": "Kicker",
    "slides": [
        {"heading": "Uno adaptado", "body": "Cuerpo uno."},
        {"heading": "Dos adaptado", "body": "Cuerpo dos."},
        {"heading": "Tres adaptado", "body": "Cuerpo tres."},
    ],
}

CHAT_PAYLOAD = {
    "conversacion": [
        {"de": "otro", "texto": "acabo de editar un video en 3 minutos"},
        {"de": "usuario", "texto": "jaja como? si tu no editas"},
        {"de": "otro", "texto": "la IA lo hace todo, yo solo grabo"},
        {"de": "usuario", "texto": "y los cortes y la musica?"},
        {"de": "otro", "texto": "tambien, de principio a fin"},
    ]
}


class FakeCtx:
    def __init__(self, tmp_path, *, plan=PLAN, voice="voice", transcript=TRANSCRIPT, palette=PALETTE):
        self.video_id = "9102_short"
        self.run_dir = tmp_path / "run"
        self.out_dir = tmp_path / "out"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._voice = (self.run_dir / "voice.m4a") if voice else None
        if self._voice:
            self._voice.write_bytes(b"x")
        self._plan, self._transcript, self._palette = plan, transcript, palette

    def carousel_plan(self):
        return self._plan

    def voice(self):
        return self._voice

    def transcript(self):
        return self._transcript

    def palette(self):
        return self._palette


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No test may hit a real agent CLI, the music library, or stale memos."""
    vc._reset_slides_memo()
    monkeypatch.setattr(vc, "_call_slides_agent", lambda *a, **k: (_ for _ in ()).throw(AdaptError("off")))
    monkeypatch.setattr(vc, "_call_chat_agent", lambda *a, **k: (_ for _ in ()).throw(AdaptError("off")))
    monkeypatch.setattr(vc, "_pick_music", lambda ctx: None)
    yield
    vc._reset_slides_memo()


# --- plan extraction -------------------------------------------------------

def test_plan_slides_splits_hero_and_items():
    title, eyebrow, items = vc._plan_slides(PLAN)
    assert title == "Crea contenido sin editores"
    assert eyebrow == "El sistema"
    assert [it["index"] for it in items] == [1, 2, 3]
    assert items[0] == {"heading": "La pereza te frena", "body": "No lo haces por pereza.", "index": 1, "total": 3}


def test_item_texts_join_heading_body():
    _, _, items = vc._plan_slides(PLAN)
    assert vc._item_texts(items)[0] == "La pereza te frena No lo haces por pereza."


# --- voz / musica duration tables -----------------------------------------

def test_voz_slide_windows_time_to_span_starts():
    # Hero shows until the first item is spoken; each item until the NEXT one
    # starts; the last holds to the end of the voice. The voice is never cut.
    wins = vc.voz_slide_windows([3.0, 4.0, 5.0], 10.0)
    assert wins == [3.0, 1.0, 1.0, 5.0]


def test_voz_slide_windows_tile_the_full_voice():
    # Core invariant: one window per slide (hero + N items) and they cover the
    # WHOLE voice with no cutting — sum == voice_dur, every window positive.
    starts = [2.0, 5.5, 9.0, 14.0]
    voice_dur = 20.0
    wins = vc.voz_slide_windows(starts, voice_dur)
    assert len(wins) == len(starts) + 1
    assert all(w > 0 for w in wins)
    assert round(sum(wins), 3) == voice_dur


def test_musica_durations():
    assert vc.musica_durations(3) == [2.5, 3.5, 3.5, 3.5]
    assert vc.musica_durations(0) == [2.5]


# --- teclado schedules -----------------------------------------------------

def test_teclado_schedule_is_cumulative_with_dwell_and_transition():
    slides = [{"heading": "AB", "body": ""}, {"heading": "CD", "body": ""}]
    sched = vc.teclado_schedule(slides, cps=22, tmin=1.8, tmax=7.0, dwell=2.5, slide_sec=0.4)
    # First window starts after one entrance beat (slide_sec).
    # 2 chars -> below tmin -> clamped to 1.8s typing, +2.5 dwell.
    assert sched[0] == {"start": 0.4, "typeEnd": 2.2, "end": 4.7}
    # next slide starts after end + entrance gap (0.4).
    assert sched[1]["start"] == 5.1
    assert sched[1]["typeEnd"] == 6.9 and sched[1]["end"] == 9.4


def test_teclado_schedule_typedur_scales_and_clamps_high():
    slides = [{"heading": "x" * 400, "body": ""}]  # huge -> clamp to tmax
    sched = vc.teclado_schedule(slides, tmax=7.0)
    assert round(sched[0]["typeEnd"] - sched[0]["start"], 3) == 7.0


def test_teclado_schedule_first_window_starts_after_entrance_beat():
    sched = vc.teclado_schedule([{"heading": "Hola", "body": ""}])
    assert sched[0]["start"] == vc.SLIDE_SEC


def test_teclado_voz_schedule_syncs_typing_to_span_starts():
    slides = [
        {"heading": "Titulo", "body": "kicker"},
        {"heading": "Uno", "body": "b1"},
        {"heading": "Dos", "body": "b2"},
    ]
    spans = [(2.0, 5.0), (8.0, 9.0)]
    sched = vc.teclado_voz_schedule(slides, spans, 12.0)
    assert len(sched) == 3  # hero + 2 spans
    # Each slide types WHEN its span is spoken (hero during the intro); the voice
    # is never cut, so the windows tile the whole voice end to end.
    assert [s["start"] for s in sched] == [0.0, 2.0, 8.0]
    assert sched[-1]["end"] == 12.0
    assert all(s["typeEnd"] > s["start"] for s in sched)
    assert all(s["end"] >= s["typeEnd"] for s in sched)


# --- whatsapp chat schedule --------------------------------------------------

def test_whatsapp_messages_prefix_title():
    _, _, items = vc._plan_slides(PLAN)
    msgs = vc.whatsapp_messages("Titulo", items)
    assert msgs[0] == "Titulo"
    assert msgs[1] == "La pereza te frena: No lo haces por pereza."


def test_whatsapp_chat_schedule_windows_per_side():
    convo = [
        {"de": "otro", "texto": "hola"},                 # indicator window = otro_lead
        {"de": "usuario", "texto": "x" * 24},            # 24 chars / 12 cps = 2.0s typing
        {"de": "otro", "texto": "y" * 90},               # long message stretches the gap after
    ]
    chat, total = vc.whatsapp_chat_schedule(
        convo, base=2.2, read_cps=30.0, lead=0.8,
        type_cps=12.0, type_min=1.2, type_max=5.0, otro_lead=1.1, tail=2.5,
    )
    assert [c["de"] for c in chat] == ["otro", "usuario", "otro"]
    assert chat[0] == {"de": "otro", "texto": "hola", "start": 0.8, "typeEnd": 1.9}
    # next event after typeEnd + reading gap max(2.2, 4/30).
    assert chat[1]["start"] == 4.1 and chat[1]["typeEnd"] == 6.1
    assert chat[2]["start"] == 8.3 and chat[2]["typeEnd"] == 9.4
    # long last message: gap = 90/30 = 3.0 > base, then the trailing hold.
    assert total == 9.4 + 3.0 + 2.5


def test_whatsapp_chat_schedule_clamps_usuario_typing():
    short, _ = vc.whatsapp_chat_schedule([{"de": "usuario", "texto": "hey"}])
    long, _ = vc.whatsapp_chat_schedule([{"de": "usuario", "texto": "z" * 300}])
    assert round(short[0]["typeEnd"] - short[0]["start"], 3) == vc.WA_TYPE_MIN
    assert round(long[0]["typeEnd"] - long[0]["start"], 3) == vc.WA_TYPE_MAX


def test_whatsapp_chat_schedule_is_ordered():
    chat, total = vc.whatsapp_chat_schedule([{"de": d, "texto": "mensaje de prueba"} for d in ("otro", "usuario") * 4])
    times = [c["start"] for c in chat] + [total]
    assert all(a < b for a, b in zip(times, times[1:]))
    assert all(c["typeEnd"] > c["start"] for c in chat)


# --- conversation validation + cache ----------------------------------------

def test_validate_conversation_cleans_and_orders():
    convo = vc._validate_conversation(CHAT_PAYLOAD)
    assert len(convo) == 5
    assert convo[0] == {"de": "otro", "texto": "acabo de editar un video en 3 minutos"}
    assert {c["de"] for c in convo} == {"usuario", "otro"}


def test_validate_conversation_drops_junk_and_truncates():
    data = {
        "conversacion": [
            {"de": "otro", "texto": "uno"},
            {"de": "robot", "texto": "lado invalido"},      # dropped
            {"texto": "sin lado"},                            # dropped
            "no es objeto",                                   # dropped
            {"de": "usuario", "texto": "  dos   con   espacios  "},
            {"de": "otro", "texto": ""},                     # dropped
            {"de": "usuario", "texto": "palabra " * 40},     # truncated
            {"de": "otro", "texto": "cuatro"},
        ]
    }
    convo = vc._validate_conversation(data)
    assert len(convo) == 4
    assert convo[1]["texto"] == "dos con espacios"
    assert len(convo[2]["texto"]) <= vc.MAX_CHAT_MSG_CHARS


def test_validate_conversation_rejects_too_few_and_clamps_count():
    with pytest.raises(AdaptError):
        vc._validate_conversation({"conversacion": [{"de": "otro", "texto": "solo uno"}]})
    with pytest.raises(AdaptError):
        vc._validate_conversation({"conversacion": "no list"})
    many = {"conversacion": [{"de": "otro", "texto": f"m{i}"} for i in range(30)]}
    assert len(vc._validate_conversation(many)) == vc.CHAT_MAX_MSGS


def test_whatsapp_conversation_calls_agent_and_caches(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    calls = {"n": 0}

    def fake(ctx_, text):
        calls["n"] += 1
        return CHAT_PAYLOAD

    monkeypatch.setattr(vc, "_call_chat_agent", fake)
    convo = vc.whatsapp_conversation(ctx)
    assert convo and convo[0]["de"] == "otro" and calls["n"] == 1

    cache = json.loads((ctx.run_dir / "_intermediates" / vc.CHAT_CACHE_NAME).read_text("utf-8"))
    assert cache["conversacion"] == convo

    # Second call must come from the disk cache, not the agent.
    monkeypatch.setattr(vc, "_call_chat_agent", lambda *a: (_ for _ in ()).throw(AssertionError("re-called")))
    assert vc.whatsapp_conversation(ctx) == convo
    assert calls["n"] == 1


def test_whatsapp_conversation_agent_failure_returns_none(tmp_path):
    ctx = FakeCtx(tmp_path)  # autouse fixture makes the agent raise
    assert vc.whatsapp_conversation(ctx) is None
    assert not (ctx.run_dir / "_intermediates" / vc.CHAT_CACHE_NAME).exists()


def test_whatsapp_conversation_needs_transcript(tmp_path):
    ctx = FakeCtx(tmp_path, transcript=None)
    assert vc.whatsapp_conversation(ctx) is None


# --- adapted slides (change #2) ----------------------------------------------

def test_adapted_slide_deck_success_memo_and_cache(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    title, eyebrow, items = vc._plan_slides(PLAN)
    calls = {"n": 0}

    def fake(ctx_, t, e, its):
        calls["n"] += 1
        return SLIDES_PAYLOAD

    monkeypatch.setattr(vc, "_call_slides_agent", fake)
    a_title, a_eyebrow, a_items = vc.adapted_slide_deck(ctx, title, eyebrow, items)
    assert a_title == "Titulo adaptado" and a_eyebrow == "Kicker"
    assert [it["heading"] for it in a_items] == ["Uno adaptado", "Dos adaptado", "Tres adaptado"]
    # index/total (the sync metadata) always comes from the plan.
    assert [it["index"] for it in a_items] == [1, 2, 3]
    assert all(it["total"] == 3 for it in a_items)

    # Memoized across consumers; persisted for a fresh process.
    vc.adapted_slide_deck(ctx, title, eyebrow, items)
    assert calls["n"] == 1
    disk = json.loads((ctx.run_dir / "_intermediates" / vc.SLIDES_CACHE_NAME).read_text("utf-8"))
    assert disk["slides"][0]["heading"] == "Uno adaptado"
    vc._reset_slides_memo()
    monkeypatch.setattr(vc, "_call_slides_agent", lambda *a: (_ for _ in ()).throw(AssertionError("re-called")))
    assert vc.adapted_slide_deck(ctx, title, eyebrow, items)[0] == "Titulo adaptado"


def test_adapted_slide_deck_count_mismatch_falls_back(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    title, eyebrow, items = vc._plan_slides(PLAN)
    bad = {**SLIDES_PAYLOAD, "slides": SLIDES_PAYLOAD["slides"][:2]}  # 2 != 3
    monkeypatch.setattr(vc, "_call_slides_agent", lambda *a: bad)
    assert vc.adapted_slide_deck(ctx, title, eyebrow, items) == (title, eyebrow, items)
    assert not (ctx.run_dir / "_intermediates" / vc.SLIDES_CACHE_NAME).exists()


def test_adapted_slide_deck_agent_failure_falls_back(tmp_path):
    ctx = FakeCtx(tmp_path)  # autouse fixture makes the agent raise
    title, eyebrow, items = vc._plan_slides(PLAN)
    assert vc.adapted_slide_deck(ctx, title, eyebrow, items) == (title, eyebrow, items)


def test_normalize_slides_truncates_to_caps():
    _, _, items = vc._plan_slides(PLAN)
    data = {
        "title": "t" * 200,
        "eyebrow": "e" * 100,
        "slides": [
            {"heading": "palabra " * 20, "body": "cuerpo " * 40},
            {"heading": "ok", "body": ""},
            {"heading": "tres", "body": "fin."},
        ],
    }
    out = vc._normalize_slides(data, "T", "E", items)
    assert len(out["slides"]) == 3
    assert len(out["slides"][0]["heading"]) <= vc.MAX_SLIDE_HEADING_CHARS
    assert len(out["slides"][0]["body"]) <= vc.MAX_SLIDE_BODY_CHARS
    assert len(out["title"]) <= vc.MAX_SLIDE_TITLE_CHARS
    assert len(out["eyebrow"]) <= vc.MAX_SLIDE_EYEBROW_CHARS


def test_normalize_slides_requires_headings():
    _, _, items = vc._plan_slides(PLAN)
    data = {"slides": [{"heading": "", "body": "x"}, {"heading": "b", "body": ""}, {"heading": "c", "body": ""}]}
    with pytest.raises(AdaptError):
        vc._normalize_slides(data, "T", "E", items)


# --- ffmpeg command construction (monkeypatched subprocess) ----------------

def _capture_run(monkeypatch):
    calls = []
    monkeypatch.setattr(vc, "_run", lambda cmd: calls.append(cmd))
    return calls


def test_mux_command(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    out = vc._mux_audio(tmp_path / "v.mp4", tmp_path / "a.m4a", tmp_path / "final.mp4")
    assert out == tmp_path / "final.mp4"
    cmd = " ".join(calls[0])
    assert "-map 0:v" in cmd and "-map 1:a" in cmd
    assert "-c:v copy" in cmd and "-c:a aac" in cmd and "-shortest" in cmd


def test_make_silence_command(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    vc._make_silence(tmp_path / "s.m4a", 3.0)
    assert "anullsrc=r=48000:cl=stereo" in " ".join(calls[0]) and "-t 3.000" in " ".join(calls[0])


def test_teclado_audio_command_has_keyboard_and_slide(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(vc, "_sfx_gain", lambda *a, **k: -3.0)
    sched = [
        {"start": 0.0, "typeEnd": 2.0, "end": 4.5},
        {"start": 4.9, "typeEnd": 6.9, "end": 9.4},
    ]
    vc._build_teclado_audio(sched, 9.4, tmp_path)
    cmd = " ".join(calls[0])
    assert "-stream_loop -1" in cmd                     # keyboard looped
    assert "atrim=0:2.000" in cmd                       # trimmed to first typing window
    assert "adelay=4900|4900" in cmd                    # slide advance at first end
    assert "amix=inputs=4:normalize=0:duration=first" in cmd  # 2 kbd + 1 slide + base
    assert "-t 9.400" in cmd


def test_teclado_audio_mixes_music_bed(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(vc, "_sfx_gain", lambda *a, **k: -6.0)
    sched = [{"start": 0.65, "typeEnd": 2.0, "end": 4.5}]
    vc._build_teclado_audio(sched, 4.5, tmp_path, music=tmp_path / "bed.mp3")
    cmd = " ".join(calls[0])
    assert str(tmp_path / "bed.mp3") in cmd
    assert "atrim=0:4.500" in cmd and "afade=t=in:st=0" in cmd and "afade=t=out:st=3.500" in cmd
    assert "[bed]" in cmd
    assert "amix=inputs=3:normalize=0:duration=first" in cmd  # 1 kbd + bed + base


def test_teclado_voz_audio_uses_full_voice_and_keyboard(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(vc, "_sfx", lambda name: tmp_path / name)
    monkeypatch.setattr(vc, "_sfx_gain", lambda *a, **k: -6.0)
    sched = [
        {"start": 0.0, "typeEnd": 1.8, "end": 2.0},   # hero (intro)
        {"start": 2.0, "typeEnd": 3.8, "end": 8.0},   # span 0 slide
        {"start": 8.0, "typeEnd": 9.8, "end": 12.0},  # span 1 slide
    ]
    vc._build_teclado_voz_audio(tmp_path / "voice.m4a", sched, 12.0, tmp_path, music=tmp_path / "bed.mp3")
    cmd = " ".join(calls[0])
    # The FULL voice is input 0 and is NEVER cut into spans.
    assert str(tmp_path / "voice.m4a") in cmd
    assert "asplit=2[voz][vsc]" in cmd                       # split for the sidechain duck
    # Keyboard taps ride during each typing window (delayed to its start).
    assert "adelay=0|0" in cmd and "adelay=2000|2000" in cmd and "adelay=8000|8000" in cmd
    assert "[bed]" in cmd                                     # ducked music bed
    # voice + 3 keyboard windows + bed = 5 mix inputs.
    assert "amix=inputs=5:normalize=0:duration=first" in cmd


def test_whatsapp_audio_taps_pop_notification_and_bed(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(vc, "_sfx_gain", lambda *a, **k: -4.0)
    chat = [
        {"de": "usuario", "texto": "hola", "start": 0.8, "typeEnd": 2.0},
        {"de": "otro", "texto": "que tal", "start": 4.2, "typeEnd": 5.3},
    ]
    vc._build_whatsapp_audio(chat, 8.0, tmp_path, music=tmp_path / "bed.mp3")
    cmd = " ".join(calls[0])
    assert "Phone Keyboard Typing.wav" in cmd
    assert "atrim=0:1.200" in cmd                    # taps trimmed to the typing window
    assert "adelay=800|800" in cmd                   # taps start with the typing
    assert "adelay=4200|4200" in cmd                 # pop when the indicator appears
    assert "adelay=5300|5300" in cmd                 # notification at the arrival
    assert "[bed]" in cmd
    assert "amix=inputs=5" in cmd                    # taps + pop + notif + bed + base


def test_whatsapp_audio_without_music(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(vc, "_sfx_gain", lambda *a, **k: -4.0)
    chat = [
        {"de": "otro", "texto": "uno", "start": 0.8, "typeEnd": 1.9},
        {"de": "otro", "texto": "dos", "start": 4.1, "typeEnd": 5.2},
    ]
    vc._build_whatsapp_audio(chat, 8.0, tmp_path)
    cmd = " ".join(calls[0])
    assert "amix=inputs=5" in cmd                    # 2 pop + 2 notif + base
    assert "Phone Keyboard Typing.wav" not in cmd    # no usuario events -> no taps


def test_duck_bed_command_sidechains_under_voice(tmp_path):
    cmd = vc._duck_bed_command(tmp_path / "voz.m4a", tmp_path / "bed.mp3", -12.0, 20.0, tmp_path / "out.m4a")
    joined = " ".join(cmd)
    assert "-stream_loop -1" in joined
    assert "sidechaincompress" in joined
    assert "volume=-12.0dB" in joined
    assert "amix=inputs=2:normalize=0:duration=first" in joined
    assert "-t 20.000" in joined


def test_mix_music_under_voice_passthrough_and_mix(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    voice_audio = tmp_path / "voz_audio.m4a"
    # No music (autouse fixture): the voice track passes through untouched.
    assert vc._mix_music_under_voice(ctx, voice_audio, 10.0, tmp_path) == voice_audio
    # With music: one ffmpeg mix, output beside the input.
    calls = _capture_run(monkeypatch)
    monkeypatch.setattr(vc, "_pick_music", lambda c: tmp_path / "bed.mp3")
    monkeypatch.setattr(vc, "_sfx_gain", lambda *a, **k: -20.0)
    out = vc._mix_music_under_voice(ctx, voice_audio, 10.0, tmp_path)
    assert out == tmp_path / "voz_audio_bed.m4a"
    assert "sidechaincompress" in " ".join(calls[0])


# --- producer wiring + skips ----------------------------------------------

def test_produce_voz_1_wires_render_and_returns_output(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    monkeypatch.setattr(vc, "map_slides_to_spans", lambda texts, tr, vid: [(0.0, 4.0), (5.0, 9.0), (10.0, 20.0)])
    monkeypatch.setattr(vc, "probe_duration", lambda p: 20.0)
    # The audio IS the full voice track (never cut); music mix is a pass-through here.
    mixed = {}
    monkeypatch.setattr(vc, "_mix_music_under_voice", lambda c, voice, secs, work: (mixed.update(voice=voice, secs=secs), voice)[1])
    captured = {}

    def fake_render(**kw):
        captured.update(kw)
        kw["out_mp4"].write_bytes(b"v")
        return kw["out_mp4"]

    monkeypatch.setattr(vc, "render_format_video", fake_render)
    monkeypatch.setattr(vc, "_mux_audio", lambda video, audio, out: out)

    out = vc.produce_voz_1(ctx)
    assert out == [ctx.out_dir / "videocarrusel_voz_1.mp4"]
    assert captured["component_file"] == "formats/SlideshowCarousel"
    assert captured["props"]["styleVariant"] == "card"
    # durationsSec includes the hero window; one entry per slide.
    assert len(captured["props"]["durationsSec"]) == 1 + 3
    # The whole-canvas sweep is gone (element-level motion lives in the comp).
    assert "sweepSec" not in captured["props"]
    assert captured["width"] == 1080 and captured["height"] == 1920
    # Slides tile the FULL voice and the full voice track feeds the mix (no cut).
    assert round(sum(captured["props"]["durationsSec"]), 3) == 20.0
    assert mixed["voice"] == ctx.voice() and mixed["secs"] == 20.0


def test_produce_voz_spans_use_original_texts_display_uses_adapted(monkeypatch, tmp_path):
    """THE sync rule: adapted texts are display-only; span mapping sees the plan."""
    ctx = FakeCtx(tmp_path)
    seen = {}

    def fake_map(texts, tr, vid):
        seen["texts"] = list(texts)
        return [(0.0, 4.0), (5.0, 9.0), (10.0, 20.0)]

    monkeypatch.setattr(vc, "map_slides_to_spans", fake_map)
    monkeypatch.setattr(vc, "_call_slides_agent", lambda *a: SLIDES_PAYLOAD)
    monkeypatch.setattr(vc, "probe_duration", lambda p: 20.0)
    monkeypatch.setattr(vc, "_mix_music_under_voice", lambda c, voice, secs, work: voice)
    captured = {}
    monkeypatch.setattr(vc, "render_format_video", lambda **kw: (captured.update(kw), kw["out_mp4"])[1])
    monkeypatch.setattr(vc, "_mux_audio", lambda v, a, out: out)

    vc.produce_voz_1(ctx)
    _, _, items = vc._plan_slides(PLAN)
    assert seen["texts"] == vc._item_texts(items)  # ORIGINAL plan wording
    assert [s["heading"] for s in captured["props"]["slides"]] == ["Uno adaptado", "Dos adaptado", "Tres adaptado"]
    assert captured["props"]["title"] == "Titulo adaptado"


def test_produce_voz_2_is_editorial(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    monkeypatch.setattr(vc, "map_slides_to_spans", lambda *a: [(0.0, 4.0), (5.0, 9.0), (10.0, 20.0)])
    monkeypatch.setattr(vc, "probe_duration", lambda p: 20.0)
    monkeypatch.setattr(vc, "_mix_music_under_voice", lambda c, voice, secs, work: voice)
    captured = {}
    monkeypatch.setattr(vc, "render_format_video", lambda **kw: (captured.update(kw), kw["out_mp4"])[1])
    monkeypatch.setattr(vc, "_mux_audio", lambda v, a, out: out)
    vc.produce_voz_2(ctx)
    assert captured["props"]["styleVariant"] == "editorial"


def test_produce_voz_skips_without_voice(tmp_path):
    ctx = FakeCtx(tmp_path, voice=None)
    assert vc.produce_voz_1(ctx) == []


def test_produce_voz_skips_without_plan(tmp_path):
    ctx = FakeCtx(tmp_path, plan=None)
    assert vc.produce_voz_1(ctx) == []


def test_produce_musica_skips_without_plan(tmp_path):
    ctx = FakeCtx(tmp_path, plan=None)
    assert vc.produce_musica(ctx) == []


def test_produce_teclado_emits_one_clip_per_idea(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    monkeypatch.setattr(vc, "_build_teclado_audio", lambda sched, secs, work, music=None: work / "a.m4a")
    captured = []

    def fake_render(**kw):
        captured.append(kw)
        kw["out_mp4"].write_bytes(b"v")
        return kw["out_mp4"]

    monkeypatch.setattr(vc, "render_format_video", fake_render)
    monkeypatch.setattr(vc, "_mux_audio", lambda v, a, out: out)
    out = vc.produce_teclado(ctx)
    # One ~10s typing clip per big idea (3 plan items -> 3 clips).
    assert out == [ctx.out_dir / f"videocarrusel_teclado_{i}.mp4" for i in (1, 2, 3)]
    for kw in captured:
        assert kw["component_file"] == "formats/TypingCarousel"
        assert kw["props"]["cursor"] is True
        # each clip is a SINGLE idea typed in, held to ~10s.
        assert len(kw["props"]["schedule"]) == 1
        assert len(kw["props"]["slides"]) == 1
        assert kw["props"]["schedule"][0]["end"] >= vc.TECLADO_CLIP_SEC


def test_produce_whatsapp_fallback_is_one_sided(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)  # agent raises (autouse) -> plan-derived fallback
    monkeypatch.setattr(vc, "_build_whatsapp_audio", lambda chat, secs, work, music=None: work / "a.m4a")
    captured = {}
    monkeypatch.setattr(vc, "render_format_video", lambda **kw: (captured.update(kw), kw["out_mp4"])[1])
    monkeypatch.setattr(vc, "_mux_audio", lambda v, a, out: out)
    out = vc.produce_whatsapp(ctx)
    assert out == [ctx.out_dir / "videocarrusel_whatsapp.mp4"]
    assert captured["component_file"] == "formats/WhatsappCarousel"
    chat = captured["props"]["chat"]
    # title + 3 items = 4 "otro" messages, plus the appended follow CTA = 5.
    assert len(chat) == 5
    assert all(ev["de"] == "otro" for ev in chat)
    assert chat[-1]["texto"] == vc.WA_CTA
    assert all(ev["typeEnd"] > ev["start"] for ev in chat)


def test_produce_whatsapp_uses_agent_conversation(monkeypatch, tmp_path):
    ctx = FakeCtx(tmp_path)
    monkeypatch.setattr(vc, "_call_chat_agent", lambda ctx_, text: CHAT_PAYLOAD)
    monkeypatch.setattr(vc, "_build_whatsapp_audio", lambda chat, secs, work, music=None: work / "a.m4a")
    captured = {}
    monkeypatch.setattr(vc, "render_format_video", lambda **kw: (captured.update(kw), kw["out_mp4"])[1])
    monkeypatch.setattr(vc, "_mux_audio", lambda v, a, out: out)
    vc.produce_whatsapp(ctx)
    chat = captured["props"]["chat"]
    # agent convo (5) + appended follow CTA ("otro") = 6, closing on the CTA.
    assert [ev["de"] for ev in chat] == ["otro", "usuario", "otro", "usuario", "otro", "otro"]
    assert chat[0]["texto"] == "acabo de editar un video en 3 minutos"
    assert chat[-1]["texto"] == vc.WA_CTA


def test_produce_teclado_voz_skips_without_transcript(tmp_path):
    ctx = FakeCtx(tmp_path, transcript=None)
    assert vc.produce_teclado_voz(ctx) == []
