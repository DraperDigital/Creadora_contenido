"""SRT subtitles from the CUT (clean) caption cues.

Module named `formats_srt` (not `srt`) to avoid confusion with the third-party
`srt` package. The cues come from `captions_props.json` — the proofread,
timeline of the cut video — NOT the raw word transcript.
"""
from __future__ import annotations

from contenido_bionico.short.formats import atoms


def _timecode(seconds: float) -> str:
    """`HH:MM:SS,mmm` SRT timecode from a float second offset."""
    ms_total = int(round(max(0.0, seconds) * 1000))
    hours, ms_total = divmod(ms_total, 3_600_000)
    minutes, ms_total = divmod(ms_total, 60_000)
    secs, ms = divmod(ms_total, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def cues_to_srt(cues: list[dict]) -> str:
    """Render caption cues `[{"text","start","end"}]` to an SRT document.

    Blocks numbered from 1, blank-line separated, single trailing newline.
    """
    blocks: list[str] = []
    for i, cue in enumerate(cues, start=1):
        start = _timecode(float(cue.get("start", 0.0)))
        end = _timecode(float(cue.get("end", 0.0)))
        text = str(cue.get("text", "")).strip()
        blocks.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def produce_srt(ctx) -> list:
    """Write `subtitulos.srt` into `ctx.out_dir` from the run's caption cues."""
    cues = atoms.caption_cues(ctx.run_dir)
    if not cues:
        return []
    out = ctx.out_dir / "subtitulos.srt"
    out.write_text(cues_to_srt(cues), encoding="utf-8")
    return [out]
