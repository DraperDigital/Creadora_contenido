"""Video-treatment producers (category `videos`).

Four alternate cuts of the same short, all 1080x1920 h264 @30fps, built with
ffmpeg over the run's existing atoms (source.mp4, captions.webm, the normalized
voice track, the camera, the music bed) plus one new alpha overlay (HeroOverlay):

  - clip_subtitulado : camera base + captions, NO animations. Voice + bed.
                       (Falls back to the static scale/pad base when the run has
                       no transcript — the registry does not require one.)
  - video_herotext   : camera talking head that HARD-CUTS to full-frame solid
                       brand-color cards on the 3-6 most impactful statements
                       (hero_phrases mode="cutaway"); voice keeps playing under
                       the cards. NO captions/scenes.
  - video_splitscreen: speaker top half + hero-text band over a brand color.

The deterministic command/graph builders are pure and unit-tested; the producers
wire atoms + renders + one ffmpeg encode around them. Loudness matches the main
reel: the voice atom is already normalized to VOICE_TARGET_LUFS, the music bed
carries the plan's gain toward MUSIC_TARGET_LUFS and ducks under the voice via
the shared sidechain filter, and every master bus is nudged to MASTER_TARGET and
capped by the same limiter.

Every producer follows the contract: `produce_<x>(ctx) -> list[Path]` — render
into `ctx.out_dir`, return published paths; raise on failure (the runner logs and
continues); return [] to skip (missing atoms / too few photos).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from contenido_bionico.shared import camera
from contenido_bionico.shared.audio.audio_mix import _DUCK_FILTER
from contenido_bionico.shared.ffmpeg import (
    MASTER_PEAK_LIMIT,
    MASTER_TARGET_LUFS,
    NO_WINDOW,
    VOICE_TARGET_LUFS,
    X264_BASELINE,
    probe_duration,
)
from contenido_bionico.shared.remotion_manifest_formats import render_format_overlay
from contenido_bionico.short.formats.hero_phrases import _word_rows, hero_phrases

OUT_W = 1080
OUT_H = 1920
OUT_FPS = 30
SPLIT_H = 960                 # top / bottom band height for the split screen
HERO_COMPOSITION_ID = "hero-overlay"
HERO_COMPONENT = "formats/HeroOverlay"

# Voice atoms are already at VOICE_TARGET_LUFS, so every master bus only needs
# the same +2 LU nudge and -1 dBTP limiter the main reel's audio mix applies.
_MASTER_GAIN_DB = round(MASTER_TARGET_LUFS - VOICE_TARGET_LUFS, 2)
_LIMITER = f"alimiter=limit={MASTER_PEAK_LIMIT}:level=false"


class VideoTreatmentError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers ---

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _probe_duration(path: Path) -> float:
    """Media duration in seconds (wrapper so tests monkeypatch one call)."""
    return probe_duration(path)


def _hex_to_ff(color: str) -> str:
    """`#RRGGBB` -> ffmpeg's `0xRRGGBB`; pass named/other colors through."""
    c = str(color).strip().lstrip("#")
    if re.fullmatch(r"[0-9A-Fa-f]{6}", c):
        return "0x" + c.upper()
    return str(color)


def _music_from_plan(run_dir: Path) -> tuple[str, float] | None:
    """(`file`, `base_gain_db`) from Audio_Plan.json when the music file exists.

    Returns None when there is no music entry or the referenced file is absent
    (a copied run may point at another checkout's library) — the treatment then
    plays voice-only rather than failing.
    """
    data = _read_json(run_dir / "Audio_Plan.json")
    if not isinstance(data, dict):
        return None
    music = data.get("music")
    if not isinstance(music, dict):
        return None
    music_file = music.get("file")
    if not music_file or not Path(music_file).exists():
        return None
    try:
        gain = float(music.get("base_gain_db"))
    except (TypeError, ValueError):
        gain = 0.0
    return str(music_file), gain


# --- filter-graph fragments ---------------------------------------------------

def _base_norm(idx: int, label: str, w: int = OUT_W, h: int = OUT_H) -> str:
    """Opaque source normalize: scale/pad (black) to w x h @fps."""
    return (
        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={OUT_FPS}[{label}]"
    )


def _overlay_norm(idx: int, label: str, w: int = OUT_W, h: int = OUT_H) -> str:
    """Transparent overlay normalize (captions/hero webm): pad w/ black@0.0."""
    return (
        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
        f"setsar=1,fps={OUT_FPS},setpts=PTS-STARTPTS[{label}]"
    )


def _overlay(prev: str, layer: str, out: str) -> str:
    return (
        f"[{prev}][{layer}]"
        f"overlay=0:0:format=auto:eof_action=pass:repeatlast=0[{out}]"
    )


def _voice_music_audio(
    voice_idx: int, music_idx: int | None, music_gain_db: float, duration: float,
    *, out_label: str = "aout",
) -> list[str]:
    """Audio chain: normalized voice + optional ducked music bed -> master.

    Without music the voice is only mastered (gain + limiter). With music the
    voice is split, the bed is trimmed/faded to `duration` at its plan gain and
    ducked under the voice via the shared sidechain filter, then both are mixed
    and mastered.
    """
    if music_idx is None:
        return [f"[{voice_idx}:a]aresample=48000,volume={_MASTER_GAIN_DB}dB,{_LIMITER}[{out_label}]"]
    fade_out_st = max(duration - 2.0, 0.0)
    return [
        f"[{voice_idx}:a]aresample=48000,asplit=2[hv_voice][hv_vsc]",
        (
            f"[{music_idx}:a]aresample=48000,atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS,volume={music_gain_db}dB,"
            f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_st:.3f}:d=2[hv_music_pre]"
        ),
        f"[hv_music_pre][hv_vsc]{_DUCK_FILTER}[hv_music]",
        (
            f"[hv_voice][hv_music]amix=inputs=2:normalize=0:dropout_transition=0,"
            f"volume={_MASTER_GAIN_DB}dB,{_LIMITER}[{out_label}]"
        ),
    ]


# --- per-treatment full graphs (pure, testable) -------------------------------

def _build_clip_subtitulado_graph(
    *, camera_chain: str | None = None, has_music: bool, music_gain_db: float, duration: float
) -> str:
    """Camera base (when a chain is given) + captions overlay + voice/music.

    `camera_chain` is the same `[0:v] -> [base]` chain herotext uses; None keeps
    the static scale/pad base (runs without a transcript — the registry does not
    require one for this format).
    """
    parts = [
        camera_chain if camera_chain else _base_norm(0, "base"),
        _overlay_norm(1, "caps"),
        _overlay("base", "caps", "vout"),
        *_voice_music_audio(2, 3 if has_music else None, music_gain_db, duration),
    ]
    return ";\n".join(parts)


def _build_herotext_graph(*, camera_chain: str, has_music: bool, music_gain_db: float, duration: float) -> str:
    parts = [
        camera_chain,                       # [0:v] -> [base]
        _overlay_norm(1, "hero"),
        _overlay("base", "hero", "vout"),
        *_voice_music_audio(2, 3 if has_music else None, music_gain_db, duration),
    ]
    return ";\n".join(parts)


def _build_splitscreen_graph(
    *, top_chain: str, bg_ff: str, has_music: bool, music_gain_db: float,
    duration: float, bottom_is_anim: bool = False,
) -> str:
    # vstack requires both inputs to share a pixel format, so the bottom band and
    # the camera top band are both pinned to yuv420p before stacking.
    if bottom_is_anim:
        # The lower half runs the run's animated scenes (input 1, a looped concat
        # of the opaque scene webms) — constant motion, like the final reel — cover-
        # cropped to the band and trimmed to the talking head's length.
        bottom = (
            f"[1:v]scale={OUT_W}:{SPLIT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{SPLIT_H},setsar=1,fps={OUT_FPS},"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=yuv420p[bottom]"
        )
    else:
        # Fallback (no animated scenes in the run): the hero-text band over a
        # solid brand color (input 1 = the HeroOverlay alpha webm).
        bottom = (
            f"color=c={bg_ff}:s={OUT_W}x{SPLIT_H}:r={OUT_FPS}:d={duration + 1:.3f}[bg];"
            + _overlay_norm(1, "herob", OUT_W, SPLIT_H)
            + ";[bg][herob]overlay=0:0:format=auto:eof_action=pass:repeatlast=0,format=yuv420p[bottom]"
        )
    parts = [
        top_chain,                          # [0:v] -> [toph] (1080x960)
        bottom,
        "[toph]format=yuv420p[topf]",
        "[topf][bottom]vstack=inputs=2,setsar=1[vout]",
        *_voice_music_audio(2, 3 if has_music else None, music_gain_db, duration),
    ]
    return ";\n".join(parts)


def _author_split_bottoms(video_id: str, duration: float):
    """Author + render the run's 1080x960 split-screen bottom panels as a
    FULL-COVERAGE band over `duration` seconds (scene panels at their exact
    windows + authored gap panels in between), returning the concatenated mp4
    (or None). A thin module-level wrapper so the heavy orchestrator import
    stays lazy AND tests can monkeypatch this single seam."""
    from contenido_bionico.short.animate.orchestrator import author_split_bottoms
    return author_split_bottoms(video_id, duration)


def _concat_bottom_animation(run_dir: Path) -> Path | None:
    """Concatenate the run's animated scene webms (`animations/<n>/animation.webm`)
    into one clip for the split-screen bottom band, or None when the run has no
    animated scenes. Same codec params across scenes -> stream copy."""
    anim_dir = run_dir / "animations"
    if not anim_dir.is_dir():
        return None
    scene_dirs = [p for p in anim_dir.iterdir() if p.is_dir()]
    scene_dirs.sort(key=lambda p: int(p.name) if p.name.isdigit() else 1 << 30)
    segs = [p / "animation.webm" for p in scene_dirs]
    segs = [s for s in segs if s.exists() and s.stat().st_size > 0]
    if not segs:
        return None
    out = run_dir / "_intermediates" / "splitscreen_bottom.webm"
    out.parent.mkdir(parents=True, exist_ok=True)
    listfile = out.with_suffix(".txt")
    listfile.write_text(
        "".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8"
    )
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(out)], cwd=run_dir)
    return out if out.exists() and out.stat().st_size > 0 else None


# --- encode -------------------------------------------------------------------

def _run_ffmpeg(args: list[str], cwd: Path) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise VideoTreatmentError(
            f"ffmpeg failed (exit {proc.returncode}). Cmd: {' '.join(cmd)}\n"
            f"stderr:\n{proc.stderr}"
        )


def _encode(ctx, inputs: list[str], graph: str, out: Path) -> None:
    """Write the filter script, run one ffmpeg encode, verify a non-empty file."""
    log_dir = ctx.run_dir / "logs" / "formats"
    log_dir.mkdir(parents=True, exist_ok=True)
    script = log_dir / f"{out.stem}.filter.txt"
    script.write_text(graph, encoding="utf-8")
    args = [
        *inputs,
        "-filter_complex_script", str(script),
        "-map", "[vout]", "-map", "[aout]",
        *X264_BASELINE,
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(OUT_FPS),
        "-movflags", "+faststart",
        "-shortest",
        str(out),
    ]
    _run_ffmpeg(args, cwd=ctx.out_dir)
    if not out.exists() or out.stat().st_size == 0:
        raise VideoTreatmentError(f"ffmpeg succeeded but output missing/empty: {out}")


def _render_hero_overlay(ctx, phrases: list[dict], zone: str, height: int, duration: float, out_webm: Path) -> Path:
    return render_format_overlay(
        composition_id=HERO_COMPOSITION_ID,
        component_file=HERO_COMPONENT,
        width=OUT_W,
        height=height,
        duration_frames=max(1, round(duration * OUT_FPS)),
        props={"phrases": phrases, "zone": zone, "durationSec": duration, "palette": ctx.palette()},
        out_webm=out_webm,
    )


# ------------------------------------------------------------------ producers -

def produce_clip_subtitulado(ctx):
    """Camera base + captions.webm overlay, voice + ducked music.

    The camera is the same treatment herotext uses (plan from the transcript's
    word timings). The registry does NOT require a transcript for this format,
    so a run without one gracefully falls back to the static no-camera base.
    """
    source, captions, voice = ctx.source(), ctx.captions_webm(), ctx.voice()
    if not (source and captions and voice):
        return []
    out = ctx.out_dir / "clip_subtitulado.mp4"
    duration = _probe_duration(source)
    camera_chain = None
    words = _word_rows(ctx.transcript())
    if words:
        plan = camera.plan_camera(words, duration, "short")
        camera_chain = camera.source_chain(plan, out_w=OUT_W, out_h=OUT_H, fps=OUT_FPS)
    music = _music_from_plan(ctx.run_dir)
    inputs = ["-i", str(source), "-c:v", "libvpx-vp9", "-i", str(captions), "-i", str(voice)]
    if music:
        inputs += ["-stream_loop", "-1", "-i", music[0]]
    graph = _build_clip_subtitulado_graph(
        camera_chain=camera_chain, has_music=bool(music),
        music_gain_db=(music[1] if music else 0.0), duration=duration,
    )
    _encode(ctx, inputs, graph, out)
    return [out]


def produce_video_herotext(ctx):
    """Camera talking head + solid-card cutaways on the most impactful lines.

    During each selected span the HeroOverlay webm is a full-bleed OPAQUE brand
    card (so the overlay hard-cuts away from the talking head); between spans it
    is fully transparent and the camera base shows. Voice + ducked music run
    uninterrupted underneath. No captions/scenes.
    """
    source, voice, transcript = ctx.source(), ctx.voice(), ctx.transcript()
    if not (source and voice and transcript):
        return []
    out = ctx.out_dir / "video_herotext.mp4"
    duration = _probe_duration(source)
    words = _word_rows(transcript)
    phrases = hero_phrases(ctx, mode="cutaway")
    hero_webm = ctx.run_dir / "_intermediates" / "hero_full.webm"
    _render_hero_overlay(ctx, phrases, "full", OUT_H, duration, hero_webm)
    plan = camera.plan_camera(words, duration, "short")
    camera_chain = camera.source_chain(plan, out_w=OUT_W, out_h=OUT_H, fps=OUT_FPS)
    music = _music_from_plan(ctx.run_dir)
    inputs = ["-i", str(source), "-c:v", "libvpx-vp9", "-i", str(hero_webm), "-i", str(voice)]
    if music:
        inputs += ["-stream_loop", "-1", "-i", music[0]]
    graph = _build_herotext_graph(
        camera_chain=camera_chain, has_music=bool(music),
        music_gain_db=(music[1] if music else 0.0), duration=duration,
    )
    _encode(ctx, inputs, graph, out)
    return [out]


def produce_video_splitscreen(ctx):
    """Speaker top half + animated panels running constantly on the bottom half,
    vstacked. The bottom band is AUTHORED for the 1080x960 panel by a dedicated
    author (`short_author_split`) as FULL COVERAGE of the whole timeline — scene
    panels at their exact windows plus gap panels for the stretches in between —
    so the animation always tracks what is being said (never a free-running
    loop). Degrades gracefully: if authoring is unavailable it crops + loops the
    full-frame scenes, and with no scenes at all it falls back to the hero-text
    band over a solid brand color."""
    source, voice, transcript = ctx.source(), ctx.voice(), ctx.transcript()
    if not (source and voice and transcript):
        return []
    out = ctx.out_dir / "video_splitscreen.mp4"
    duration = _probe_duration(source)
    words = _word_rows(transcript)
    palette = ctx.palette()
    plan = camera.plan_camera(words, duration, "short")
    top_chain = camera.source_chain_split_top(plan, out_w=OUT_W, out_h=SPLIT_H, fps=OUT_FPS)
    music = _music_from_plan(ctx.run_dir)

    # Prefer panels authored for the 1080x960 band (h264 mp4); fall back to the
    # cropped full-frame scene concat (vp9 webm), then to the hero-text band.
    bottom_anim = None
    bottom_is_vp9 = True
    try:
        authored = _author_split_bottoms(ctx.video_id, duration)
    except Exception as exc:  # noqa: BLE001 — authoring must never break the treatment
        print(f"[formats] splitscreen author unavailable: {str(exc)[:200]}", flush=True)
        authored = None
    if authored is not None:
        bottom_anim, bottom_is_vp9 = authored, False
    else:
        bottom_anim = _concat_bottom_animation(ctx.run_dir)

    inputs = ["-i", str(source)]
    if bottom_anim is not None:
        # Input 1: the bottom-panel band. The authored band already covers the
        # whole video in sync; -stream_loop matters only for the cropped-scene
        # fallback (partial coverage) and is a harmless safety net otherwise
        # (-shortest ends the encode at the video/voice length).
        if bottom_is_vp9:
            inputs += ["-stream_loop", "-1", "-c:v", "libvpx-vp9", "-i", str(bottom_anim)]
        else:
            inputs += ["-stream_loop", "-1", "-i", str(bottom_anim)]
    else:
        # Fallback: render the hero-text band as input 1.
        hero_webm = ctx.run_dir / "_intermediates" / "hero_bottom.webm"
        _render_hero_overlay(ctx, hero_phrases(ctx), "bottom", SPLIT_H, duration, hero_webm)
        inputs += ["-c:v", "libvpx-vp9", "-i", str(hero_webm)]
    inputs += ["-i", str(voice)]
    if music:
        inputs += ["-stream_loop", "-1", "-i", music[0]]

    graph = _build_splitscreen_graph(
        top_chain=top_chain, bg_ff=_hex_to_ff(palette["bg"]), has_music=bool(music),
        music_gain_db=(music[1] if music else 0.0), duration=duration,
        bottom_is_anim=bottom_anim is not None,
    )
    _encode(ctx, inputs, graph, out)
    return [out]
