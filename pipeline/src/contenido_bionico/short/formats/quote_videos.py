"""Quote-video producers (category `citas`).

Two deliverables, both best-effort (`produce_<x>(ctx) -> list[Path]`; return []
to skip, raise to let the runner log + continue):

- `produce_quote_foto`  -> `quote_foto_N.mp4` (x0-3): a quote-over-photo still
  looped into a 10s mp4 with a faded, loudness-normalized music bed. The still
  is reused from `cita_foto_N.png` (WS5's static producer) when already on disk,
  otherwise rendered here through the SAME `formats/FraseImagen` comp WS5 ships
  (style "foto") using the run's ADAPTED quote texts (`frase_adapt.adapted_citas`
  — the same cached call the cita_foto stills use, so both paths render the same
  words; raw quotes on any agent failure). The 10s loop reuses the EXISTING
  `make_quote_video` recipe from the quote flow verbatim (imported, not
  re-implemented).

- `produce_quote_karaoke` -> `quote_karaoke_N.mp4` (x0-3): only for quotes that
  match the transcript VERBATIM (always the RAW quote-plan texts — the word sync
  depends on them; never the adapted rewrites). The matched transcript words are
  sliced and re-zeroed to the span start, the creator's REAL voice for that span
  is extracted, `KaraokeQuote` animates the words one-by-one, and the two are
  muxed — with the run's Audio_Plan music as a low bed ducked under the voice
  (same sidechain + master-bus discipline as the video treatments) when the plan
  has one, voice-only otherwise. A quote that is paraphrased (no verbatim span)
  is skipped; zero matches is the correct, non-error outcome (returns []).
"""
from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path

from contenido_bionico.shared.audio.audio_mix import _DUCK_FILTER
from contenido_bionico.shared.ffmpeg import NO_WINDOW
from contenido_bionico.shared.remotion_manifest_formats import (
    render_format_stills,
    render_format_video,
)
from contenido_bionico.shared.remotion_manifest_writer import DEFAULT_FPS as FPS
from contenido_bionico.shared.render_remotion import REMOTION_DIR
from contenido_bionico.short.formats import broll, frase_adapt
from contenido_bionico.short.formats import video_carousels as _vc
from contenido_bionico.short.formats.span_map import extract_audio_span, find_verbatim_span
from contenido_bionico.short.formats.video_treatments import (
    _LIMITER,
    _MASTER_GAIN_DB,
    _music_from_plan,
)
from contenido_bionico.short.quote import build_quotes

# Remotion static-asset root: photos are staged under
# public/assets/runs/<video_id>/broll/ so FraseImagen's staticFile(photoSrc)
# resolves during the still render (same per-run-namespaced convention WS5 uses
# for its photo formats). Module-level so tests can retarget it off shared/.
REMOTION_PUBLIC = REMOTION_DIR / "public"

# Visual dwell after the voice ends so the last word stays readable. The karaoke
# video is intentionally this much LONGER than its voice span (see the mux note).
TAIL_DWELL_SEC = 0.8

# Quote-over-photo stills share the 4:5 canvas of the static quote images.
_FOTO_W, _FOTO_H = 1080, 1350
_MAX_QUOTES = 3


def _clean(text: object) -> str:
    """Collapse whitespace in a quote string."""
    return re.sub(r"\s+", " ", str(text or "").strip())


def _top_quotes(ctx) -> list[str]:
    """Non-empty quote texts, strongest first, capped at `_MAX_QUOTES`."""
    plan = ctx.quote_plan()
    if not isinstance(plan, dict):
        return []
    out: list[str] = []
    for item in plan.get("quotes") or []:
        text = _clean(item.get("text") if isinstance(item, dict) else item)
        if text:
            out.append(text)
        if len(out) >= _MAX_QUOTES:
            break
    return out


# --------------------------------------------------------------------------- #
# quote_foto: quote over a user photo -> 10s mp4 with music
# --------------------------------------------------------------------------- #

def _stage_photo(video_id: str, photo: Path) -> str:
    """Copy `photo` into the Remotion public assets; return its staticFile path.

    Per-run namespaced (`assets/runs/<id>/broll/<name>`) so concurrent runs never
    collide, mirroring the quote flow's template staging.
    """
    dest_dir = REMOTION_PUBLIC / "assets" / "runs" / str(video_id) / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / photo.name
    if not dest.exists() or dest.stat().st_size != photo.stat().st_size:
        dest.write_bytes(photo.read_bytes())
    return f"assets/runs/{video_id}/broll/{photo.name}"


def _render_frase_foto_still(ctx, idx: int, text: str, photo: Path, palette: dict) -> Path:
    """Render the quote-over-photo still via WS5's `formats/FraseImagen` (foto)."""
    photo_src = _stage_photo(ctx.video_id, photo)
    png = ctx.run_dir / "_intermediates" / f"quote_foto_still_{idx}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    render_format_stills(
        composition_id="frase-imagen",
        component_file="formats/FraseImagen",
        width=_FOTO_W,
        height=_FOTO_H,
        duration_frames=1,
        props={
            "style": "foto",
            "text": text,
            "attribution": "",
            "photoSrc": photo_src,
            "palette": palette,
        },
        out_pngs=[png],
        frames=[0],
    )
    return png


def produce_quote_foto(ctx) -> list[Path]:
    """Top-3 quotes x photo[i] -> `quote_foto_N.mp4` (quote-over-photo + music).

    Reuses an existing `cita_foto_N.png` when WS5's static producer already wrote
    it; otherwise renders the still here with the ADAPTED quote text
    (`frase_adapt.adapted_citas`, index-paired with the raw plan — the same
    cached call the cita_foto stills use, so both paths stay consistent; the raw
    quote on any shortfall). Skips entirely with [] when there are no quotes, no
    photos, or no music bed available.
    """
    quotes = _top_quotes(ctx)
    if not quotes:
        return []
    photos = broll.select_photos(ctx, _MAX_QUOTES, "fondo de cita")
    if not photos:
        return []
    tracks = build_quotes._music_tracks()
    if not tracks:
        # `make_quote_video` needs a music bed; degrade to skip rather than error.
        return []

    palette = ctx.palette()
    out: list[Path] = []
    adapted: list[str] | None = None  # fetched lazily: only a rendered still needs texts
    for i in range(min(len(quotes), len(photos))):
        idx = i + 1
        still = ctx.out_dir / f"cita_foto_{idx}.png"
        if not still.exists():
            if adapted is None:
                # Same count/order as the raw quote plan (frase_adapt contract),
                # so plan index i pairs the adapted text with photo[i].
                adapted = frase_adapt.adapted_citas(ctx)
            text = adapted[i] if i < len(adapted) and adapted[i] else quotes[i]
            still = _render_frase_foto_still(ctx, idx, text, photos[i], palette)
        music = random.choice(tracks)
        mp4 = ctx.out_dir / f"quote_foto_{idx}.mp4"
        build_quotes.make_quote_video(still, music, mp4)
        out.append(mp4)
    return out


# --------------------------------------------------------------------------- #
# quote_karaoke: verbatim quote, words synced to the creator's real voice
# --------------------------------------------------------------------------- #

def _span_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Transcript words inside `[start, end]`, re-zeroed to the span start.

    Each row becomes `{"word", "start", "end"}` with times relative to `start`
    (clamped at 0) — the shape `KaraokeQuote` expects.
    """
    out: list[dict] = []
    for w in words:
        if w.get("type", "word") != "word":
            continue
        try:
            w_start = float(w["start"])
            w_end = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        # Overlap with the span (endpoints inclusive within a tiny epsilon).
        if w_start >= end - 1e-6 or w_end <= start + 1e-6:
            continue
        text = _clean(w.get("text") or w.get("word"))
        if not text:
            continue
        rel_start = max(0.0, round(w_start - start, 3))
        rel_end = max(rel_start, round(w_end - start, 3))
        out.append({"word": text, "start": rel_start, "end": rel_end})
    return out


def _karaoke_music_filter(duration: float, music_gain_db: float) -> str:
    """Audio graph for the karaoke mux with a music bed -> `[aout]`.

    Mirrors the video treatments' loudness discipline: the voice span ([1:a],
    already at VOICE_TARGET_LUFS) is split so one copy keys the shared sidechain
    duck; the bed ([2:a], looped input) is trimmed to the VIDEO duration (it
    keeps playing under the tail dwell after the voice ends) at its plan gain
    with short fades; then voice + ducked bed are mixed and mastered (same +LU
    nudge and true-peak limiter as every treatment master bus). The sidechain
    copy is padded with silence past the voice end so the duck filter never
    starves while the bed plays out the dwell.
    """
    fade_out_d = min(0.6, duration / 2)
    fade_out_st = max(duration - fade_out_d, 0.0)
    return ";".join(
        [
            "[1:a]aresample=48000,asplit=2[kq_voice][kq_vsc0]",
            f"[kq_vsc0]apad=pad_dur={TAIL_DWELL_SEC + 1.0:.3f}[kq_vsc]",
            (
                f"[2:a]aresample=48000,atrim=duration={duration:.3f},"
                f"asetpts=PTS-STARTPTS,volume={music_gain_db}dB,"
                f"afade=t=in:st=0:d=0.3,afade=t=out:st={fade_out_st:.3f}:d={fade_out_d:.3f}"
                f"[kq_music_pre]"
            ),
            f"[kq_music_pre][kq_vsc]{_DUCK_FILTER}[kq_music]",
            (
                f"[kq_voice][kq_music]amix=inputs=2:normalize=0:dropout_transition=0,"
                f"volume={_MASTER_GAIN_DB}dB,{_LIMITER}[aout]"
            ),
        ]
    )


def _mux_video_audio(
    video: Path, audio: Path, out: Path,
    *, music: tuple[str, float] | None = None, duration: float | None = None,
) -> Path:
    """Mux the silent karaoke video with its voice span (+ optional music bed).

    NOTE (deviation from the plan's literal `-shortest`): the karaoke video is
    intentionally `TAIL_DWELL_SEC` LONGER than the voice span (the last word
    dwells after the voice stops), so `-shortest` would truncate that tail. The
    video is always the longer stream, so omitting `-shortest` yields exactly the
    intended length (span + tail) with the voice ending naturally into silence.

    With `music` (an Audio_Plan `(file, base_gain_db)`) and the video `duration`,
    the plan track is added as a looped third input and mixed under the voice by
    `_karaoke_music_filter` (ducked bed, trimmed to `duration`, mastered) — the
    bed carries the tail dwell instead of silence.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    if music is not None and duration is not None:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
            "-stream_loop", "-1", "-i", str(music[0]),
            "-filter_complex", _karaoke_music_filter(duration, music[1]),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out),
        ]
    subprocess.run(cmd, check=True, capture_output=True, creationflags=NO_WINDOW)
    return out


def produce_quote_karaoke(ctx) -> list[Path]:
    """Verbatim quotes -> `quote_karaoke_N.mp4` (words synced to the real voice).

    Only quotes that match the transcript verbatim produce a video; paraphrased
    quotes (no span) are skipped. Zero matches returns [] (correct, not an error).
    Capped at 3. Always matches the RAW quote-plan texts (never the adapted
    rewrites — the verbatim voice sync depends on them). The mux adds the run's
    Audio_Plan music as a low ducked bed when the plan has one.
    """
    voice = ctx.voice()
    transcript = ctx.transcript()
    quotes = _top_quotes(ctx)
    if voice is None or not isinstance(transcript, dict) or not quotes:
        return []
    words = transcript.get("words") or []
    if not words:
        return []

    music = _music_from_plan(ctx.run_dir)
    palette = ctx.palette()
    out: list[Path] = []
    idx = 0
    for text in quotes:
        if idx >= _MAX_QUOTES:
            break
        span = find_verbatim_span(text, words)
        if span is None:
            continue  # paraphrased quote: no verbatim span -> skip (not an error)
        start, end = span
        span_words = _span_words(words, start, end)
        if not span_words:
            continue
        idx += 1

        duration_sec = round((end - start) + TAIL_DWELL_SEC, 3)
        inter = ctx.run_dir / "_intermediates"
        inter.mkdir(parents=True, exist_ok=True)
        audio = inter / f"quote_karaoke_{idx}.m4a"
        extract_audio_span(voice, start, end, audio)

        silent = inter / f"quote_karaoke_{idx}.silent.mp4"
        render_format_video(
            composition_id="karaoke-quote",
            component_file="formats/KaraokeQuote",
            width=1080,
            height=1920,
            duration_frames=max(1, round(duration_sec * FPS)),
            props={
                "text": text,
                "words": span_words,
                "durationSec": duration_sec,
                "palette": palette,
            },
            out_mp4=silent,
        )

        final = ctx.out_dir / f"quote_karaoke_{idx}.mp4"
        _mux_video_audio(silent, audio, final, music=music, duration=duration_sec)
        out.append(final)
    return out


# --------------------------------------------------------------------------- #
# quote_tecleado: the quote TYPES in (keyboard SFX) + highlighter pen -> mp4
# --------------------------------------------------------------------------- #

_TEC_W, _TEC_H = 1080, 1920
_TECLADO_CLIP_SEC = 10.0
_TECLADO_TYPE_FRAC = 0.62  # fraction of the clip spent typing, the rest holds
_FUNCTION_WORDS = {
    "de", "la", "el", "los", "las", "un", "una", "que", "y", "o", "en", "a",
    "su", "sus", "tu", "tus", "lo", "es", "no", "con", "por", "para", "se",
    "le", "les", "al", "del", "mi", "mis", "si", "ya", "me", "te", "nos",
}


def _highlight_words(text: str, k: int = 2) -> list[str]:
    """The `k` longest CONTENT words (function words skipped) — the word(s) the
    highlighter pen marks. Deterministic, no agent call."""
    words = re.findall(r"[0-9A-Za-zÁÉÍÓÚÑáéíóúñü]+", text)
    content = [w for w in words if w.lower() not in _FUNCTION_WORDS and len(w) >= 4]
    content.sort(key=len, reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for w in content:
        if w.lower() in seen:
            continue
        seen.add(w.lower())
        out.append(w)
        if len(out) >= k:
            break
    return out


def _tecleado_texts(ctx) -> list[str]:
    """Memorable quote texts for the tecleado clips: the adapted citas (same
    cached call the cita images use), falling back to the raw plan quote."""
    try:
        adapted = frase_adapt.adapted_citas(ctx)
    except Exception:  # noqa: BLE001 — best-effort: fall back to raw quotes
        adapted = []
    raw = _top_quotes(ctx)
    out: list[str] = []
    for i in range(len(raw)):
        alt = _clean(adapted[i]) if i < len(adapted) else ""
        out.append(alt or raw[i])
    return out[:_MAX_QUOTES]


def produce_quote_tecleado(ctx) -> list[Path]:
    """Top quotes -> `quote_tecleado_N.mp4`: the quote TYPES in (keyboard SFX)
    with a highlighter-pen swipe on the key word(s), over a music bed. The typed
    counterpart to the photo/static quote posts, so each quote gets 2 formats."""
    texts = _tecleado_texts(ctx)
    if not texts:
        return []
    palette = ctx.palette()
    music = _vc._pick_music(ctx)
    seconds = _TECLADO_CLIP_SEC
    frames = round(seconds * FPS)
    type_end = round(seconds * _TECLADO_TYPE_FRAC, 3)
    outputs: list[Path] = []
    for i, text in enumerate(texts, start=1):
        highlights = _highlight_words(text)
        work = _vc._work(ctx, f"quote_tecleado_{i}")
        video = render_format_video(
            composition_id="quote-tecleado",
            component_file="formats/QuoteTecleado",
            width=_TEC_W, height=_TEC_H, duration_frames=frames,
            props={"text": text, "highlights": highlights,
                   "typeEndSec": type_end, "palette": palette},
            out_mp4=work / "video.mp4",
        )
        # Keyboard taps during the typing window [0, type_end] + a music bed.
        schedule = [{"start": 0.0, "typeEnd": type_end, "end": seconds}]
        audio = _vc._build_teclado_audio(schedule, seconds, work, music=music)
        outputs.append(_vc._mux_audio(video, audio, ctx.out_dir / f"quote_tecleado_{i}.mp4"))
    return outputs
