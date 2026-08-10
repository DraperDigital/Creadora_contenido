"""Animated poster videos.

Two ~8s mp4s per poster layout, mirroring the two static poster variants:
  - `poster_video_N.mp4`      SOLID: the layout's designed background + a DISTINCT
                              foreground cutout (one different person per layout).
  - `poster_video_N_foto.mp4` FOTO:  a DISTINCT complete cutout pair (background_N
                              + foreground_N), a different combo per layout.
Rendered as video so each layout's `PosterFrame` auto-animates its text groups in
with its signature entrance, over a music bed + entrance whoosh. Reuses
`posters.py`'s content author, index-aware cutout selection, and staging so each
video matches its still in wording, layering, AND cutout.
"""
from __future__ import annotations

from pathlib import Path

from contenido_bionico.shared.remotion_manifest_formats import render_format_video
from contenido_bionico.short.formats import poster_content, posters
from contenido_bionico.short.formats import video_carousels as _vc

FPS = 30
POSTER_W, POSTER_H = 1080, 1350
CLIP_SEC = 8.0
_ENTER_SFX_SEC = (0.15, 0.45)  # whoosh at the behind + front entrances


def _poster_video_audio(ctx, seconds: float, work: Path) -> Path:
    """A faded music bed + a whoosh on each text-group entrance."""
    slide = _vc._sfx("Slide.wav")
    sgain = _vc._sfx_gain(slide)
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    idx = 1
    for t in _ENTER_SFX_SEC:
        ms = int(round(t * 1000))
        inputs += ["-i", str(slide)]
        filters.append(f"[{idx}:a]volume={sgain}dB,adelay={ms}|{ms}[s{idx}]")
        labels.append(f"[s{idx}]")
        idx += 1
    _vc._append_bed(inputs, filters, labels, idx, _vc._pick_music(ctx), seconds)
    out = work / "poster_video_audio.m4a"
    _vc._run(_vc._mix_command(inputs, filters, labels, seconds, out))
    return out


def _content_or_none(ctx):
    if ctx.out_dir is None:
        return None
    content = poster_content.content(ctx)
    if not content or not content.get("headline"):
        return None
    return content


def _render_poster_video(ctx, layout: int, props: dict, comp_id: str, out_name: str) -> list[Path]:
    frames = round(CLIP_SEC * FPS)
    comp = posters._LAYOUTS[layout]
    work = _vc._work(ctx, out_name)
    video = render_format_video(
        composition_id=comp_id,
        component_file=f"formats/{comp}",
        width=POSTER_W, height=POSTER_H, duration_frames=frames,
        props=props, out_mp4=work / "video.mp4",
    )
    audio = _poster_video_audio(ctx, CLIP_SEC, work)
    return [_vc._mux_audio(video, audio, Path(ctx.out_dir) / f"{out_name}.mp4")]


def _produce_solid(ctx, layout: int) -> list[Path]:
    """SOLID animated poster (`poster_video_N.mp4`): the layout's designed
    background + a DISTINCT foreground cutout (index = layout - 1) so each of the
    four solid videos shows a different person."""
    content = _content_or_none(ctx)
    if content is None:
        return []
    props = {"content": content, "mode": "solid", "palette": ctx.palette(),
             "subjectSrc": posters._stage(ctx, posters._solid_subject(ctx, layout - 1))}
    return _render_poster_video(ctx, layout, props, f"poster-video-{layout}", f"poster_video_{layout}")


def _produce_foto(ctx, layout: int) -> list[Path]:
    """FOTO animated poster (`poster_video_N_foto.mp4`): a DISTINCT complete cutout
    PAIR (index = layout - 1) so each of the four foto videos uses a different
    background_N/foreground_N combo. Skips when the run has no complete pair."""
    content = _content_or_none(ctx)
    if content is None:
        return []
    pair = posters._foto_pair(ctx, layout - 1)
    if pair is None:
        return []
    plate, fg = pair
    props = {"content": content, "mode": "foto", "palette": ctx.palette(),
             "bgSrc": posters._stage(ctx, plate), "subjectSrc": posters._stage(ctx, fg)}
    return _render_poster_video(ctx, layout, props, f"poster-video-{layout}-foto", f"poster_video_{layout}_foto")


def produce_1(ctx) -> list[Path]: return _produce_solid(ctx, 1)
def produce_2(ctx) -> list[Path]: return _produce_solid(ctx, 2)
def produce_3(ctx) -> list[Path]: return _produce_solid(ctx, 3)
def produce_4(ctx) -> list[Path]: return _produce_solid(ctx, 4)
def produce_1_foto(ctx) -> list[Path]: return _produce_foto(ctx, 1)
def produce_2_foto(ctx) -> list[Path]: return _produce_foto(ctx, 2)
def produce_3_foto(ctx) -> list[Path]: return _produce_foto(ctx, 3)
def produce_4_foto(ctx) -> list[Path]: return _produce_foto(ctx, 4)
