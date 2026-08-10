"""Video-carousel producers (category `video_carruseles`, all 1080x1920 opaque).

Six treatments of the same key-point carousel plan:

  produce_voz_1 / produce_voz_2  slides advance under the creator's REAL voice
                                 spans (card / editorial styling)
  produce_musica                 slides auto-advance on a timer over a music bed
  produce_teclado                each slide types in (typewriter + keyboard SFX)
  produce_teclado_voz            typing paced to the creator's real voice spans
  produce_whatsapp               a generic chat mock — an agent-written two-way
                                 conversation about the video's content

Each producer follows the contract `produce_<x>(ctx) -> list[Path]`: it renders
an opaque video with `render_format_video` (h264), builds its own audio track
with ffmpeg, muxes them, and publishes one mp4 into `ctx.out_dir`. Missing atoms
=> `return []` (skip). Deterministic timing/audio-command construction lives in
the module-level helpers so it can be unit-tested without ffmpeg or a renderer.

Every producer carries MUSIC (change #7): `musica` keeps its solo bed; the
voice variants (voz_1/voz_2/teclado_voz) mix a bed ducked under the speech via
the shared sidechain filter; the SFX variants (teclado/whatsapp) mix a quieter
bed under the foreground taps/notifications.

The slideshow/typing variants DISPLAY agent-adapted slide texts (change #2,
`agents/slides_video.md`, cached per run) while `map_slides_to_spans` keeps
receiving the ORIGINAL plan texts — the 1:1 count/order guarantee keeps the
voice spans aligned with what is on screen. Any agent failure falls back to
the classic plan slides exactly as before.

The whatsapp variant (change #8) shows an agent-invented two-way conversation
(`agents/whatsapp_chat.md`) grounded in the transcript: right-side messages
type character-by-character over looping phone-keyboard taps, left-side
messages arrive after a typing indicator. Agent failure falls back to the
plan-derived one-sided arrivals.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import threading
from pathlib import Path

from contenido_bionico.shared import config
from contenido_bionico.shared.audio.audio_mix import _DUCK_FILTER
from contenido_bionico.shared.ffmpeg import (
    MUSIC_TARGET_LUFS,
    NO_WINDOW,
    SFX_TARGET_LUFS,
    measure_lufs,
    probe_duration,
    static_gain_db,
)
from contenido_bionico.shared.remotion_manifest_formats import render_format_video
from contenido_bionico.shared.remotion_runtime import REMOTION_PUBLIC
from contenido_bionico.shared.run_ids import run_number
from contenido_bionico.short.formats.adapt import (
    AdaptError,
    call_json_agent,
    clean_line,
    tagged,
    transcript_text,
)
from contenido_bionico.short.formats.span_map import map_slides_to_spans

FPS = 30
WIDTH, HEIGHT = 1080, 1920

# Voz / musica timing. Slide changes are element-level (operator motion spec:
# elements enter from their nearest edge with blur+fade, exit fade+blur inside
# their own window) — the comp reads everything off `durationsSec`.
HERO_SEC = 3.0            # hero (title) slide dwell before item spans start
GAP_S = 0.35             # inter-clip breath between concatenated voice spans
ITEM_MIN, ITEM_MAX = 2.5, 12.0   # per-item window clamp (seconds)
MUSICA_HERO_SEC = 2.5
MUSICA_ITEM_SEC = 3.5

# Typing timing.
TYPE_CPS = 22.0          # characters per second the typewriter reveals
TYPE_MIN, TYPE_MAX = 1.8, 7.0
TECLADO_DWELL = 2.5      # dwell after a slide finishes typing (keyboard variant)
TECLADO_VOZ_DWELL = 1.2  # dwell after a slide finishes typing (voice variant)
TECLADO_CLIP_SEC = 10.0  # target length of ONE per-idea typing clip (like a quote mp4)
# Gap between typed slides = the motion-spec entrance window (0.5-0.8 s): the
# incoming slide's elements enter (nearest-edge slide + blur + fade) across
# this beat and settle exactly when its typing window starts. The Slide.wav
# whoosh lands at the outgoing slide's `end`, i.e. the entrance start.
SLIDE_SEC = 0.65

# WhatsApp timing.
WA_BASE_GAP = 2.2        # minimum reading gap between chat events
WA_CPS = 30.0            # reading speed that stretches the gap for long messages
WA_LEAD = 0.8            # initial delay before the first chat event
WA_TAIL = 2.5            # hold after the last message
WA_TYPE_CPS = 12.0       # phone-typing speed for right-side ("usuario") messages
WA_TYPE_MIN, WA_TYPE_MAX = 1.2, 5.0  # usuario typing-window clamp (seconds)
WA_OTRO_LEAD = 1.1       # three-dot typing-indicator time before an "otro" arrival
WA_CTA = "Sígueme para más como esto"  # follow CTA appended as the closing chat bubble
# Chat header identity. Neutral defaults; operators brand the chat with the
# BIONICO_WA_NAME env var and by dropping a photo at broll_library/pfp.jpeg
# (or pointing BIONICO_WA_PFP at one). Absent photo -> letter avatar.
WA_CONTACT_NAME = os.environ.get("BIONICO_WA_NAME", "Contenido Biónico")
WA_CONTACT_PFP = Path(
    os.environ.get("BIONICO_WA_PFP", "")
    or config.REPO_ROOT / "broll_library" / "pfp.jpeg"
)

# The music bed is the ONLY audio in `musica`, so it sits well above the
# under-voice music target (voice target + music offset + 14 LU ~= -22 LUFS).
MUSIC_BED_TARGET_LUFS = MUSIC_TARGET_LUFS + 14.0
# Bed under foreground SFX (teclado / whatsapp): louder than an under-voice bed
# (there is no speech to protect) but clearly below SFX_TARGET_LUFS (-22) so the
# taps/notifications stay legible on top.
MUSIC_UNDER_SFX_TARGET_LUFS = MUSIC_TARGET_LUFS + 8.0
BED_FADE_IN_S = 0.8
BED_FADE_OUT_S = 1.0

_AUDIO_LIB = config.REPO_ROOT / "audio_library"
_AGENTS_DIR = Path(__file__).resolve().parent / "agents"

# Slide adaptation (change #2): one cached agent call per run, consumed by
# voz_1/voz_2/musica/teclado/teclado_voz.
SLIDES_PROMPT_PATH = _AGENTS_DIR / "slides_video.md"
SLIDES_CACHE_NAME = "adapt_slides_video.json"
SLIDES_CACHE_VERSION = 1
MAX_SLIDE_HEADING_CHARS = 40
MAX_SLIDE_BODY_CHARS = 110
MAX_SLIDE_TITLE_CHARS = 60
MAX_SLIDE_EYEBROW_CHARS = 28

# WhatsApp conversation (change #8).
CHAT_PROMPT_PATH = _AGENTS_DIR / "whatsapp_chat.md"
CHAT_CACHE_NAME = "whatsapp_chat.json"
CHAT_CACHE_VERSION = 1
CHAT_MIN_MSGS, CHAT_MAX_MSGS = 4, 16   # prompt asks 8-14; accept anything sane
MAX_CHAT_MSG_CHARS = 160

# In-process memo so pooled producers trigger at most one slides-adaptation
# call per run, even before the disk cache exists.
_SLIDES_MEMO: dict[str, dict | None] = {}
_SLIDES_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# small shared utilities
# --------------------------------------------------------------------------

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, creationflags=NO_WINDOW)


def _work(ctx, name: str) -> Path:
    """Per-producer scratch dir (namespaced so pooled producers never collide)."""
    d = ctx.run_dir / "_intermediates" / "video_carousels" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sfx(name: str) -> Path:
    return _AUDIO_LIB / "sfx" / name


def _sfx_gain(path: Path, target: float = SFX_TARGET_LUFS) -> float:
    """Static dB gain that brings audio asset `path` to `target` LUFS (0.0 if unmeasurable)."""
    return static_gain_db(measure_lufs(path), target)


def _truncate_words(text: str, limit: int) -> str:
    """Cut `text` to <= `limit` chars at a word boundary (no trailing ellipsis)."""
    text = clean_line(text)
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut[:limit].strip()


def _plan_slides(plan: dict) -> tuple[str, str, list[dict]]:
    """(title, eyebrow, item_slides[{heading,body,index,total}]) from a carousel plan."""
    slides = plan.get("slides") or []
    hero = next((s for s in slides if s.get("kind") == "hero"), None)
    items = [s for s in slides if s.get("kind") == "item"]
    title = str((hero or {}).get("title") or plan.get("title") or "").strip()
    eyebrow = str((hero or {}).get("eyebrow") or "").strip()
    total = len(items)
    out: list[dict] = []
    for i, s in enumerate(items):
        out.append(
            {
                "heading": str(s.get("heading") or "").strip(),
                "body": str(s.get("body") or "").strip(),
                "index": int(s.get("index") or (i + 1)),
                "total": int(s.get("total") or total),
            }
        )
    return title, eyebrow, out


def _item_texts(items: list[dict]) -> list[str]:
    return [f"{it['heading']} {it['body']}".strip() for it in items]


def _mux_audio(video: Path, audio: Path, out: Path) -> Path:
    """Combine an opaque video stream with an audio track into `out` (stream-copy video)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y", "-i", str(video), "-i", str(audio),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(out),
        ]
    )
    return out


def _make_silence(out: Path, seconds: float) -> Path:
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-t", f"{seconds:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
            "-c:a", "aac", "-b:a", "192k", str(out),
        ]
    )
    return out


# --------------------------------------------------------------------------
# music beds (change #7: every carousel carries music)
# --------------------------------------------------------------------------

def _pick_music(ctx) -> Path | None:
    """Music file for this run: the Audio_Plan pick if present, else a stable
    per-run choice from the shorts library."""
    plan = _read_json(ctx.run_dir / "Audio_Plan.json")
    if isinstance(plan, dict):
        music = plan.get("music")
        if isinstance(music, dict) and music.get("file"):
            candidate = Path(str(music["file"]))
            if candidate.exists():
                return candidate
    shorts = sorted((_AUDIO_LIB / "music" / "shorts").glob("*.mp3"))
    if not shorts:
        return None
    try:
        seed = run_number(ctx.video_id)
    except (TypeError, ValueError):
        seed = abs(hash(str(ctx.video_id))) & 0xFFFFFFFF
    return shorts[random.Random(seed).randrange(len(shorts))]


def _build_music_bed(music: Path, seconds: float, work: Path) -> Path:
    gain = static_gain_db(measure_lufs(music), MUSIC_BED_TARGET_LUFS)
    fade_out_start = max(0.0, seconds - 1.0)
    af = (
        f"volume={gain}dB,"
        f"afade=t=in:st=0:d=0.8,"
        f"afade=t=out:st={fade_out_start:.3f}:d=1.0"
    )
    out = work / "music_bed.m4a"
    _run(
        [
            # -vn drops any embedded cover-art image: mp3s with cover art expose
            # a video stream that ffmpeg would otherwise try to mux into the .m4a
            # (h264-in-ipod -> "codec not supported in container", encode fails).
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(music), "-vn",
            "-t", f"{seconds:.3f}", "-af", af,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out),
        ]
    )
    return out


def _bed_filter(idx: int, seconds: float, gain_db: float, label: str = "bed") -> str:
    """Filter chain for input `idx` (the looped music): trim to `seconds`, set
    the bed gain, fade in/out -> `[label]`."""
    fade_out_start = max(0.0, seconds - BED_FADE_OUT_S)
    return (
        f"[{idx}:a]aresample=48000,atrim=0:{seconds:.3f},asetpts=PTS-STARTPTS,"
        f"volume={gain_db}dB,afade=t=in:st=0:d={BED_FADE_IN_S},"
        f"afade=t=out:st={fade_out_start:.3f}:d={BED_FADE_OUT_S}[{label}]"
    )


def _append_bed(
    inputs: list[str], filters: list[str], labels: list[str],
    idx: int, music: Path | None, seconds: float,
) -> int:
    """Append a music bed (under-SFX target) as one more amix source. Returns
    the next free input index (unchanged when there is no music)."""
    if music is None:
        return idx
    gain = _sfx_gain(music, MUSIC_UNDER_SFX_TARGET_LUFS)
    inputs += ["-stream_loop", "-1", "-i", str(music)]
    filters.append(_bed_filter(idx, seconds, gain))
    labels.append("[bed]")
    return idx + 1


def _duck_bed_command(
    voice_audio: Path, music: Path, gain_db: float, seconds: float, out: Path
) -> list[str]:
    """ffmpeg command mixing a looped music bed UNDER `voice_audio`, ducked by
    the shared sidechain filter (same policy as the main reel / treatments)."""
    graph = ";".join(
        [
            "[0:a]aresample=48000,asplit=2[voz][vsc]",
            _bed_filter(1, seconds, gain_db, "bed_pre"),
            f"[bed_pre][vsc]{_DUCK_FILTER}[bed]",
            "[voz][bed]amix=inputs=2:normalize=0:duration=first[out]",
        ]
    )
    return [
        "ffmpeg", "-y", "-i", str(voice_audio),
        "-stream_loop", "-1", "-i", str(music),
        "-filter_complex", graph,
        "-map", "[out]", "-t", f"{seconds:.3f}",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out),
    ]


def _mix_music_under_voice(ctx, voice_audio: Path, seconds: float, work: Path) -> Path:
    """Voice carousels carry a music bed ducked under the speech (change #7).

    Returns the mixed track, or `voice_audio` untouched when the run has no
    music to pick from.
    """
    music = _pick_music(ctx)
    if music is None:
        return voice_audio
    gain = _sfx_gain(music, MUSIC_TARGET_LUFS)
    out = work / f"{voice_audio.stem}_bed.m4a"
    _run(_duck_bed_command(voice_audio, music, gain, seconds, out))
    return out


# --------------------------------------------------------------------------
# adapted slide texts (change #2) — one cached agent call per run
# --------------------------------------------------------------------------

def _reset_slides_memo() -> None:
    """Test hook: forget every memoized slides payload."""
    with _SLIDES_LOCK:
        _SLIDES_MEMO.clear()


def _slides_fingerprint(title: str, eyebrow: str, items: list[dict]) -> str:
    source = "\n".join([title, eyebrow, *_item_texts(items)])
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def _call_slides_agent(ctx, title: str, eyebrow: str, items: list[dict]) -> dict:
    """The single slides-adaptation call. Isolated so tests monkeypatch one seam."""
    text = transcript_text(ctx.transcript())
    if not text:
        raise AdaptError("transcript has no spoken text")
    puntos = "\n".join(
        [
            f"Portada: {eyebrow} - {title}".strip(),
            *(f"{i}. {it['heading']} - {it['body']}".strip() for i, it in enumerate(items, start=1)),
        ]
    )
    message = "\n".join(
        [
            "Run the Slides Video job.",
            "Return only the JSON object on stdout. Do not use tools or read files.",
            tagged("TRANSCRIPT", text),
            tagged("PUNTOS", puntos),
        ]
    )
    return call_json_agent(
        ctx, prompt_path=SLIDES_PROMPT_PATH, log_name="slides_video", message=message
    )


def _normalize_slides(data: dict, title: str, eyebrow: str, items: list[dict]) -> dict:
    """Validate/clamp the agent payload into the canonical cached shape.

    Raises AdaptError unless `slides` has EXACTLY one entry per plan item (the
    1:1 count/order guarantee is what keeps voice spans aligned).
    """
    slides_in = data.get("slides")
    if not isinstance(slides_in, list) or len(slides_in) != len(items):
        got = len(slides_in) if isinstance(slides_in, list) else "none"
        raise AdaptError(f"slides count {got} != {len(items)} plan items")
    slides: list[dict] = []
    for entry in slides_in:
        if not isinstance(entry, dict):
            raise AdaptError("slide entry is not an object")
        heading = _truncate_words(clean_line(entry.get("heading")), MAX_SLIDE_HEADING_CHARS)
        body = _truncate_words(clean_line(entry.get("body")), MAX_SLIDE_BODY_CHARS)
        if not heading:
            raise AdaptError("slide heading empty")
        slides.append({"heading": heading, "body": body})
    return {
        "version": SLIDES_CACHE_VERSION,
        "fuente": _slides_fingerprint(title, eyebrow, items),
        "title": _truncate_words(clean_line(data.get("title")), MAX_SLIDE_TITLE_CHARS) or title,
        "eyebrow": _truncate_words(clean_line(data.get("eyebrow")), MAX_SLIDE_EYEBROW_CHARS) or eyebrow,
        "slides": slides,
    }


def _slides_cache_path(ctx) -> Path:
    return ctx.run_dir / "_intermediates" / SLIDES_CACHE_NAME


def _read_slides_cache(ctx, fingerprint: str) -> dict | None:
    data = _read_json(_slides_cache_path(ctx))
    if not isinstance(data, dict) or data.get("version") != SLIDES_CACHE_VERSION:
        return None
    if data.get("fuente") != fingerprint or not isinstance(data.get("slides"), list):
        return None
    return data


def _write_intermediate(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # the cache is an optimization; never fail the payload over it


def _slides_payload(ctx, title: str, eyebrow: str, items: list[dict]) -> dict | None:
    """The normalized slides adaptation for this run, or None on any failure.

    Memoized per (run_dir, plan fingerprint); persisted to
    `_intermediates/adapt_slides_video.json` so the five consumers spend one
    agent call between them and a replanned run re-adapts.
    """
    fingerprint = _slides_fingerprint(title, eyebrow, items)
    key = f"{ctx.run_dir}|{fingerprint}"
    with _SLIDES_LOCK:
        if key in _SLIDES_MEMO:
            return _SLIDES_MEMO[key]
        cached = _read_slides_cache(ctx, fingerprint)
        if cached is not None:
            _SLIDES_MEMO[key] = cached
            return cached
        try:
            payload = _normalize_slides(_call_slides_agent(ctx, title, eyebrow, items), title, eyebrow, items)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            print(f"[formats] slides_video no disponible, uso slides originales ({exc})",
                  file=sys.stderr, flush=True)
            _SLIDES_MEMO[key] = None
            return None
        _write_intermediate(_slides_cache_path(ctx), payload)
        _SLIDES_MEMO[key] = payload
        return payload


def adapted_slide_deck(ctx, title: str, eyebrow: str, items: list[dict]) -> tuple[str, str, list[dict]]:
    """(title, eyebrow, items) adapted for on-screen slideshow/typewriter reading.

    Items keep the plan's index/total (same count and order — the sync
    guarantee); any failure returns the originals untouched. NOTE: display
    only. `map_slides_to_spans` must keep receiving `_item_texts(items)` of the
    ORIGINAL plan items.
    """
    payload = _slides_payload(ctx, title, eyebrow, items)
    if payload is None:
        return title, eyebrow, items
    adapted = [
        {**it, "heading": s["heading"], "body": s["body"]}
        for it, s in zip(items, payload["slides"])
    ]
    return payload["title"], payload["eyebrow"], adapted


# --------------------------------------------------------------------------
# voz_1 / voz_2 — real-voice slideshow
# --------------------------------------------------------------------------

def voz_slide_windows(starts: list[float], voice_dur: float) -> list[float]:
    """Per-slide display windows [hero, item_0, item_1, ...] timed ON TOP of the
    FULL, uninterrupted voice: the hero (title) shows until the first item is
    spoken, each item shows until the NEXT item starts, and the last item holds
    to the end of the voice. The voice is NEVER cut — the slides are timed to it.

    `starts` are the transcript start times of the item spans (aligned to the
    clean cut). Boundaries are sanitized to be strictly increasing within
    (0, voice_dur) so the windows always sum to `voice_dur` and stay positive.
    """
    bounds = [0.0]
    prev = 0.0
    for s in starts:
        s = min(max(float(s), prev + 0.05), voice_dur)
        bounds.append(s)
        prev = s
    bounds.append(voice_dur)
    return [round(b - a, 3) for a, b in zip(bounds, bounds[1:])]


def _produce_voz(ctx, *, variant: str, out_name: str, key: str) -> list[Path]:
    plan = ctx.carousel_plan()
    voice = ctx.voice()
    transcript = ctx.transcript()
    if not plan or voice is None or transcript is None:
        return []
    title, eyebrow, items = _plan_slides(plan)
    if not items:
        return []
    # SYNC RULE: spans always map against the ORIGINAL plan texts; the adapted
    # texts are display-only (same count/order keeps slide N on span N).
    spans = map_slides_to_spans(_item_texts(items), transcript, ctx.video_id)
    voice_dur = probe_duration(voice)
    # Slides are timed to the FULL voice: it plays uninterrupted and each slide
    # changes exactly when the next spoken part starts — the voice is never cut.
    durations = voz_slide_windows([s for s, _e in spans], voice_dur)
    frames = round(voice_dur * FPS)
    disp_title, disp_eyebrow, disp_items = adapted_slide_deck(ctx, title, eyebrow, items)

    work = _work(ctx, key)
    # The audio IS the full clean voice (the single source of truth) with a music
    # bed ducked under it — no cutting, no re-concatenation of spans.
    audio = _mix_music_under_voice(ctx, voice, voice_dur, work)
    video = render_format_video(
        composition_id="slideshow",
        component_file="formats/SlideshowCarousel",
        width=WIDTH, height=HEIGHT, duration_frames=frames,
        props={
            "styleVariant": variant,
            "title": disp_title,
            "eyebrow": disp_eyebrow,
            "slides": disp_items,
            "durationsSec": [round(d, 3) for d in durations],
            "palette": ctx.palette(),
        },
        out_mp4=work / "video.mp4",
    )
    return [_mux_audio(video, audio, ctx.out_dir / out_name)]


def produce_voz_1(ctx) -> list[Path]:
    return _produce_voz(ctx, variant="card", out_name="videocarrusel_voz_1.mp4", key="voz_1")


def produce_voz_2(ctx) -> list[Path]:
    return _produce_voz(ctx, variant="editorial", out_name="videocarrusel_voz_2.mp4", key="voz_2")


# --------------------------------------------------------------------------
# musica — timed slideshow over a music bed
# --------------------------------------------------------------------------

def musica_durations(n_items: int) -> list[float]:
    return [MUSICA_HERO_SEC] + [MUSICA_ITEM_SEC] * n_items


def produce_musica(ctx) -> list[Path]:
    plan = ctx.carousel_plan()
    if not plan:
        return []
    title, eyebrow, items = _plan_slides(plan)
    if not items:
        return []
    durations = musica_durations(len(items))
    seconds = sum(durations)
    frames = round(seconds * FPS)
    disp_title, disp_eyebrow, disp_items = adapted_slide_deck(ctx, title, eyebrow, items)

    work = _work(ctx, "musica")
    music = _pick_music(ctx)
    audio = _build_music_bed(music, seconds, work) if music else _make_silence(work / "silent.m4a", seconds)
    video = render_format_video(
        composition_id="slideshow",
        component_file="formats/SlideshowCarousel",
        width=WIDTH, height=HEIGHT, duration_frames=frames,
        props={
            "styleVariant": "card",
            "title": disp_title,
            "eyebrow": disp_eyebrow,
            "slides": disp_items,
            "durationsSec": [round(d, 3) for d in durations],
            "palette": ctx.palette(),
        },
        out_mp4=work / "video.mp4",
    )
    return [_mux_audio(video, audio, ctx.out_dir / "videocarrusel_musica.mp4")]


# --------------------------------------------------------------------------
# teclado / teclado_voz — typewriter slideshow
# --------------------------------------------------------------------------

def _typing_slides(title: str, eyebrow: str, items: list[dict]) -> list[dict]:
    """Hero (title/eyebrow) + item slides, reduced to the {heading,body} the
    typing comp reveals."""
    slides = [{"heading": title, "body": eyebrow}]
    slides += [{"heading": it["heading"], "body": it["body"]} for it in items]
    return slides


def teclado_schedule(
    slides: list[dict],
    *,
    cps: float = TYPE_CPS,
    tmin: float = TYPE_MIN,
    tmax: float = TYPE_MAX,
    dwell: float = TECLADO_DWELL,
    slide_sec: float = SLIDE_SEC,
) -> list[dict]:
    """Per-slide {start, typeEnd, end} where type duration scales with length and
    every slide gets a fixed dwell; consecutive windows are separated by a
    `slide_sec` gap that the comp uses as the incoming slide's entrance (the
    first window starts after one such entrance beat)."""
    sched: list[dict] = []
    t = slide_sec
    for s in slides:
        chars = len(s.get("heading", "")) + len(s.get("body", ""))
        type_dur = _clamp(chars / cps, tmin, tmax)
        start = t
        type_end = start + type_dur
        end = type_end + dwell
        sched.append({"start": round(start, 3), "typeEnd": round(type_end, 3), "end": round(end, 3)})
        t = end + slide_sec
    return sched


def teclado_voz_schedule(
    slides: list[dict],
    spans,
    voice_dur: float,
    *,
    cps: float = TYPE_CPS,
    tmin: float = TYPE_MIN,
    tmax: float = TYPE_MAX,
) -> list[dict]:
    """Typing synced to the FULL, uninterrupted voice: the hero types during the
    intro (before the first span), each item types WHEN its span is spoken and
    holds until the next span starts, and the last holds to the end of the voice.
    The windows tile the whole voice, so the voice is NEVER cut — the typing rides
    on top of it. One entry per slide (hero + one per span); `slides` supplies the
    per-slide char count that paces the typing."""
    starts: list[float] = []
    prev = 0.0
    for s, _e in spans:
        s = min(max(float(s), prev + 0.05), voice_dur)
        starts.append(s)
        prev = s
    bounds = [0.0] + starts + [voice_dur]
    sched: list[dict] = []
    for i, slide in enumerate(slides):
        start = bounds[i]
        win_end = bounds[i + 1] if i + 1 < len(bounds) else voice_dur
        chars = len(slide.get("heading", "")) + len(slide.get("body", ""))
        room = max(0.3, win_end - start)
        type_dur = _clamp(chars / cps, min(tmin, room), min(tmax, room))
        type_end = min(round(start + type_dur, 3), round(win_end, 3))
        sched.append({"start": round(start, 3), "typeEnd": type_end, "end": round(win_end, 3)})
    return sched


def _mix_command(inputs: list[str], filters: list[str], labels: list[str], seconds: float, out: Path) -> list[str]:
    """Build an ffmpeg amix command over a silent base (input 0) + labelled clips."""
    filters = list(filters)
    filters.append(
        "[0:a]" + "".join(labels) + f"amix=inputs={len(labels) + 1}:normalize=0:duration=first[out]"
    )
    return [
        "ffmpeg", "-y",
        "-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-t", f"{seconds:.3f}",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out),
    ]


def _build_teclado_audio(schedule: list[dict], seconds: float, work: Path, music: Path | None = None) -> Path:
    kbd, slide = _sfx("PC Keyboard Typing.wav"), _sfx("Slide.wav")
    kbd_gain, slide_gain = _sfx_gain(kbd), _sfx_gain(slide)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    idx = 1
    for i, sc in enumerate(schedule):
        winlen = max(0.0, sc["typeEnd"] - sc["start"])
        ms = int(round(sc["start"] * 1000))
        inputs += ["-stream_loop", "-1", "-i", str(kbd)]
        filters.append(
            f"[{idx}:a]atrim=0:{winlen:.3f},asetpts=PTS-STARTPTS,"
            f"volume={kbd_gain}dB,adelay={ms}|{ms}[k{i}]"
        )
        labels.append(f"[k{i}]")
        idx += 1
    for i in range(len(schedule) - 1):
        ms = int(round(schedule[i]["end"] * 1000))
        inputs += ["-i", str(slide)]
        filters.append(f"[{idx}:a]volume={slide_gain}dB,adelay={ms}|{ms}[s{i}]")
        labels.append(f"[s{i}]")
        idx += 1
    idx = _append_bed(inputs, filters, labels, idx, music, seconds)
    out = work / "teclado_audio.m4a"
    _run(_mix_command(inputs, filters, labels, seconds, out))
    return out


def _build_teclado_voz_audio(
    voice: Path, schedule: list[dict], seconds: float, work: Path, music: Path | None = None
) -> Path:
    """The FULL continuous voice (input 0, NEVER cut) + looping keyboard taps
    during each typing window + a music bed ducked under the voice. The typing
    SFX ride on top of the uninterrupted voice — no span extraction/re-timing."""
    kbd = _sfx("PC Keyboard Typing.wav")
    kbd_gain = _sfx_gain(kbd)
    inputs: list[str] = ["-i", str(voice)]
    filters: list[str] = []
    labels: list[str] = ["[voz]"]
    # Split the voice only when there is a bed to sidechain-duck against it.
    filters.append(
        "[0:a]aresample=48000,asplit=2[voz][vsc]" if music is not None
        else "[0:a]aresample=48000[voz]"
    )
    idx = 1
    for i, sc in enumerate(schedule):
        winlen = max(0.0, sc["typeEnd"] - sc["start"])
        ms = int(round(sc["start"] * 1000))
        inputs += ["-stream_loop", "-1", "-i", str(kbd)]
        filters.append(
            f"[{idx}:a]atrim=0:{winlen:.3f},asetpts=PTS-STARTPTS,"
            f"volume={kbd_gain}dB,adelay={ms}|{ms}[k{i}]"
        )
        labels.append(f"[k{i}]")
        idx += 1
    if music is not None:
        gain = _sfx_gain(music, MUSIC_TARGET_LUFS)
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        filters.append(_bed_filter(idx, seconds, gain, "bed_pre"))
        filters.append(f"[bed_pre][vsc]{_DUCK_FILTER}[bed]")
        labels.append("[bed]")
        idx += 1
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=first[out]")
    out = work / "teclado_voz_audio.m4a"
    _run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-t", f"{seconds:.3f}",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out),
    ])
    return out


def _render_typing(ctx, slides, schedule, out_name, work) -> Path:
    frames = round(schedule[-1]["end"] * FPS)
    return render_format_video(
        composition_id="typing",
        component_file="formats/TypingCarousel",
        width=WIDTH, height=HEIGHT, duration_frames=frames,
        props={
            "slides": slides,
            "schedule": schedule,
            "cursor": True,
            "palette": ctx.palette(),
        },
        out_mp4=work / "video.mp4",
    )


def teclado_clip_schedule(
    slide: dict,
    *,
    clip_sec: float = TECLADO_CLIP_SEC,
    cps: float = TYPE_CPS,
    tmin: float = TYPE_MIN,
    tmax: float = TYPE_MAX,
    slide_sec: float = SLIDE_SEC,
) -> list[dict]:
    """One-slide typing schedule that fills a fixed ~`clip_sec` clip: a short
    entrance beat, the idea types in (duration scales with length, clamped), then
    the fully-typed idea holds until `clip_sec`. Single-window list so it plugs
    straight into `_build_teclado_audio` / `_render_typing`."""
    chars = len(slide.get("heading", "")) + len(slide.get("body", ""))
    type_dur = _clamp(chars / cps, tmin, tmax)
    start = slide_sec
    type_end = start + type_dur
    end = max(round(type_end + TECLADO_DWELL, 3), clip_sec)
    return [{"start": round(start, 3), "typeEnd": round(type_end, 3), "end": round(end, 3)}]


def produce_teclado(ctx) -> list[Path]:
    """One ~10 s typing clip PER BIG IDEA (like a static quote mp4, but typed):
    each carousel item types in over keyboard SFX + a music bed, then holds.
    Emits `videocarrusel_teclado_1.mp4`, `_2.mp4`, ... — one per idea."""
    plan = ctx.carousel_plan()
    if not plan:
        return []
    title, eyebrow, items = _plan_slides(plan)
    if not items:
        return []
    # Display uses the adapted item texts, so the typing pace matches the screen.
    _title, _eyebrow, disp_items = adapted_slide_deck(ctx, title, eyebrow, items)
    music = _pick_music(ctx)
    outputs: list[Path] = []
    for i, it in enumerate(disp_items, start=1):
        slide = {"heading": it["heading"], "body": it["body"]}
        schedule = teclado_clip_schedule(slide)
        seconds = schedule[-1]["end"]
        work = _work(ctx, f"teclado_{i}")
        audio = _build_teclado_audio(schedule, seconds, work, music=music)
        video = _render_typing(ctx, [slide], schedule, f"videocarrusel_teclado_{i}.mp4", work)
        outputs.append(_mux_audio(video, audio, ctx.out_dir / f"videocarrusel_teclado_{i}.mp4"))
    return outputs


def produce_teclado_voz(ctx) -> list[Path]:
    plan = ctx.carousel_plan()
    voice = ctx.voice()
    transcript = ctx.transcript()
    if not plan or voice is None or transcript is None:
        return []
    title, eyebrow, items = _plan_slides(plan)
    if not items:
        return []
    # SYNC RULE: spans from the ORIGINAL plan texts; adapted texts display-only.
    spans = map_slides_to_spans(_item_texts(items), transcript, ctx.video_id)
    disp_title, disp_eyebrow, disp_items = adapted_slide_deck(ctx, title, eyebrow, items)
    slides = _typing_slides(disp_title, disp_eyebrow, disp_items)
    voice_dur = probe_duration(voice)
    # Typing is synced to the FULL voice and the voice plays uncut; the schedule
    # covers the whole track so the video length == the voice length.
    schedule = teclado_voz_schedule(slides, spans, voice_dur)

    work = _work(ctx, "teclado_voz")
    # Full voice + keyboard taps during each typing window + a ducked music bed.
    audio = _build_teclado_voz_audio(voice, schedule, voice_dur, work, music=_pick_music(ctx))
    video = _render_typing(ctx, slides, schedule, "videocarrusel_teclado_voz.mp4", work)
    return [_mux_audio(video, audio, ctx.out_dir / "videocarrusel_teclado_voz.mp4")]


# --------------------------------------------------------------------------
# whatsapp — generic chat mock (agent-written two-way conversation)
# --------------------------------------------------------------------------

def whatsapp_messages(title: str, items: list[dict]) -> list[str]:
    """Fallback message texts straight from the carousel plan (one-sided)."""
    msgs = [title]
    for it in items:
        heading, body = it["heading"], it["body"]
        msgs.append(f"{heading}: {body}".strip() if body else heading)
    return [m for m in msgs if m]


def _validate_conversation(data: dict) -> list[dict]:
    """The agent's `conversacion` -> a clean [{de, texto}] list.

    Drops malformed entries, clamps message length and conversation size;
    raises AdaptError when too few valid messages survive.
    """
    msgs = data.get("conversacion")
    if not isinstance(msgs, list):
        raise AdaptError("conversacion is not a list")
    convo: list[dict] = []
    for entry in msgs:
        if not isinstance(entry, dict):
            continue
        de = str(entry.get("de") or "").strip().lower()
        texto = _truncate_words(clean_line(entry.get("texto")), MAX_CHAT_MSG_CHARS)
        if de not in ("usuario", "otro") or not texto:
            continue
        convo.append({"de": de, "texto": texto})
    if len(convo) < CHAT_MIN_MSGS:
        raise AdaptError(f"conversacion has {len(convo)} valid messages (< {CHAT_MIN_MSGS})")
    return convo[:CHAT_MAX_MSGS]


def _call_chat_agent(ctx, text: str) -> dict:
    """The single conversation call. Isolated so tests monkeypatch one seam."""
    message = "\n".join(
        [
            "Run the WhatsApp Chat job.",
            "Return only the JSON object on stdout. Do not use tools or read files.",
            tagged("TRANSCRIPT", text),
        ]
    )
    return call_json_agent(
        ctx, prompt_path=CHAT_PROMPT_PATH, log_name="whatsapp_chat", message=message
    )


def _chat_cache_path(ctx) -> Path:
    return ctx.run_dir / "_intermediates" / CHAT_CACHE_NAME


def whatsapp_conversation(ctx) -> list[dict] | None:
    """The agent-invented two-way conversation for this run, or None on failure.

    Persisted to `_intermediates/whatsapp_chat.json` (fingerprinted by the
    transcript text) so re-renders and the change agent reuse/edit one artifact
    instead of re-calling the agent.
    """
    text = transcript_text(ctx.transcript())
    if not text:
        return None
    fingerprint = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cached = _read_json(_chat_cache_path(ctx))
    if isinstance(cached, dict) and cached.get("version") == CHAT_CACHE_VERSION and cached.get("fuente") == fingerprint:
        try:
            return _validate_conversation(cached)
        except AdaptError:
            pass  # stale/hand-broken cache: fall through to a fresh call
    try:
        convo = _validate_conversation(_call_chat_agent(ctx, text))
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        print(f"[formats] whatsapp_chat no disponible, uso mensajes del plan ({exc})",
              file=sys.stderr, flush=True)
        return None
    _write_intermediate(
        _chat_cache_path(ctx),
        {"version": CHAT_CACHE_VERSION, "fuente": fingerprint, "conversacion": convo},
    )
    return convo


def whatsapp_chat_schedule(
    convo: list[dict],
    *,
    base: float = WA_BASE_GAP,
    read_cps: float = WA_CPS,
    lead: float = WA_LEAD,
    type_cps: float = WA_TYPE_CPS,
    type_min: float = WA_TYPE_MIN,
    type_max: float = WA_TYPE_MAX,
    otro_lead: float = WA_OTRO_LEAD,
    tail: float = WA_TAIL,
) -> tuple[list[dict], float]:
    """Deterministic per-message schedule [{de, texto, start, typeEnd}] + total.

    "usuario" messages TYPE on screen from `start` to `typeEnd` (window scales
    with chars/`type_cps`, clamped); "otro" messages show a typing indicator
    from `start` and arrive at `typeEnd` (fixed `otro_lead`). After each
    message the next event waits a reading gap that grows with its length; the
    total includes the last reading gap plus a trailing hold.
    """
    chat: list[dict] = []
    t = lead
    for msg in convo:
        de, texto = msg["de"], msg["texto"]
        dur = _clamp(len(texto) / type_cps, type_min, type_max) if de == "usuario" else otro_lead
        chat.append(
            {"de": de, "texto": texto, "start": round(t, 3), "typeEnd": round(t + dur, 3)}
        )
        t = t + dur + max(base, len(texto) / read_cps)
    return chat, round(t + tail, 3)


def _build_whatsapp_audio(chat: list[dict], seconds: float, work: Path, music: Path | None = None) -> Path:
    """SFX timeline for the chat: phone-keyboard taps loop under each "usuario"
    typing window; "otro" keeps the pop (indicator appears) + notification
    (arrival); plus the under-SFX music bed."""
    taps, notif, pop = _sfx("Phone Keyboard Typing.wav"), _sfx("Notification Medium.wav"), _sfx("Pop.wav")
    taps_gain, notif_gain, pop_gain = _sfx_gain(taps), _sfx_gain(notif), _sfx_gain(pop)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    idx = 1
    for i, ev in enumerate(chat):
        start_ms = int(round(ev["start"] * 1000))
        if ev["de"] == "usuario":
            winlen = max(0.0, ev["typeEnd"] - ev["start"])
            inputs += ["-stream_loop", "-1", "-i", str(taps)]
            filters.append(
                f"[{idx}:a]atrim=0:{winlen:.3f},asetpts=PTS-STARTPTS,"
                f"volume={taps_gain}dB,adelay={start_ms}|{start_ms}[k{i}]"
            )
            labels.append(f"[k{i}]")
            idx += 1
        else:
            arrive_ms = int(round(ev["typeEnd"] * 1000))
            inputs += ["-i", str(pop)]
            filters.append(f"[{idx}:a]volume={pop_gain}dB,adelay={start_ms}|{start_ms}[p{i}]")
            labels.append(f"[p{i}]")
            idx += 1
            inputs += ["-i", str(notif)]
            filters.append(f"[{idx}:a]volume={notif_gain}dB,adelay={arrive_ms}|{arrive_ms}[n{i}]")
            labels.append(f"[n{i}]")
            idx += 1
    idx = _append_bed(inputs, filters, labels, idx, music, seconds)
    out = work / "whatsapp_audio.m4a"
    _run(_mix_command(inputs, filters, labels, seconds, out))
    return out


def _stage_contact_pfp(video_id: str) -> str | None:
    """Copy the operator profile photo into this run's remotion assets and return
    its staticFile path, or None when the photo is absent (header falls back to a
    letter avatar). Staged per-run (pruned with the rest of the run's assets)."""
    if not WA_CONTACT_PFP.exists():
        return None
    dest_dir = REMOTION_PUBLIC / "assets" / "runs" / str(video_id) / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"contact_pfp{WA_CONTACT_PFP.suffix.lower()}"
    dest = dest_dir / name
    try:
        data = WA_CONTACT_PFP.read_bytes()
    except OSError:
        return None
    if not dest.exists() or dest.stat().st_size != len(data):
        dest.write_bytes(data)
    return f"assets/runs/{video_id}/broll/{name}"


def produce_whatsapp(ctx) -> list[Path]:
    plan = ctx.carousel_plan()
    if not plan:
        return []
    title, eyebrow, items = _plan_slides(plan)
    if not items:
        return []
    convo = whatsapp_conversation(ctx)
    if not convo:
        # Fallback: the classic plan-derived messages, all arriving one-sided.
        convo = [{"de": "otro", "texto": m} for m in whatsapp_messages(title, items)]
    # Close the chat on a follow CTA (item: every format that allows one gets a CTA).
    convo = list(convo) + [{"de": "otro", "texto": WA_CTA}]
    chat, seconds = whatsapp_chat_schedule(convo)
    frames = round(seconds * FPS)

    work = _work(ctx, "whatsapp")
    audio = _build_whatsapp_audio(chat, seconds, work, music=_pick_music(ctx))
    video = render_format_video(
        composition_id="whatsapp",
        component_file="formats/WhatsappCarousel",
        width=WIDTH, height=HEIGHT, duration_frames=frames,
        props={
            "chat": chat,
            "palette": ctx.palette(),
            "name": WA_CONTACT_NAME,
            "avatarSrc": _stage_contact_pfp(ctx.video_id),
        },
        out_mp4=work / "video.mp4",
    )
    return [_mux_audio(video, audio, ctx.out_dir / "videocarrusel_whatsapp.mp4")]
