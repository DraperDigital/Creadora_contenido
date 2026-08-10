"""Front-of-pipeline silence capping on the VIDEO itself.

Produces a working copy of the source video where every silence longer than
`cap_s` (default 120 ms) is shortened to `cap_s`, keeping the short beat that
sits right before the next word. The whole pipeline (long or short) then runs on
this de-silenced video: ElevenLabs transcribes it, the editor cuts it, and the
animation composites over it. Because the transcribed video IS the working
video, every timeline is native and consistent -- there is no remap step.

The cap is applied to the video once, up front; the original raw video is only
read to build the de-silenced copy and is then discarded from the rest of the
pipeline's point of view.

A short beat (`cap_s`) is kept rather than removing the pause entirely so that
ElevenLabs Scribe still tokenises each word separately instead of gluing
neighbours across a hard cut. After transcription, the trim_spacings step
removes any over-long spacing that survives (e.g. murmurs Scribe could not
detect); this step only removes the silent dead air up front.

The render is chunked and PARALLEL: the keep list is split into contiguous
chunks, each chunk is encoded by its own ffmpeg process, and the chunks are
joined at the end. By default it uses every available CPU core minus two. A
single monolithic filter graph (the old approach) runs inside ffmpeg's
single-threaded filter scheduler and its cost grows with segments x frames --
roughly quadratic in duration -- which turned long webinars into multi-hour
renders on one core.

CLI:
    python desilence.py <raw.mp4> -o <desilenced.mp4> [--workers N]

Exit codes:
    0  desilenced.mp4 written (rendered, or hardlinked when nothing to cap)
    1  ffmpeg render failed (caller should treat the run as failed)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# This file is also run by path as a child process (see shared/cut/orchestrator),
# so make the package importable before the package import below.
_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from contenido_bionico.shared.ffmpeg import (  # noqa: E402
    NO_WINDOW as _NO_WIN,
    probe_duration_or_none,
    strip_rotation_metadata,
)

CAP_S = 0.12            # default kept beat (the orchestrator overrides per call)
# -30 dB: a stretch counts as silence when it stays below this level. The value
# sits a margin ABOVE the typical noise floor of these sources: many recordings
# carry a faint background bed (room tone, low music) during pauses that hovers
# around -30 dB, so the old -35 dB default registered the whole pause as "never
# silent" and silencedetect returned nothing -- the de-silencer then hardlinked
# the source through UNCUT (every long pause survived). At -30 dB those pauses
# are detected and capped. Louder room tone, breaths and quiet word tails still
# survive as kept audio (word tails are also protected from clipping by PAD_S
# below, the kept beat on the word-tail side). Tuned empirically.
THRESHOLD_DB = -30.0
MIN_DETECT_S = 0.2      # shortest silence (seconds) ffmpeg silencedetect will flag
PAD_S = 0.05    # tight lead-in kept BEFORE the next word (the main pause sits
                # AFTER the previous word instead); render fades this in.
FADE_S = 0.008  # short audio fade in/out at each cut join -> kills the click/"pop"
                # from concatenating at a non-zero-crossing point in the waveform.
MIN_SOUND_S = 0.1  # audible regions SHORTER than this between two silences are
                   # microsounds (clicks, mouth noise, stray breaths) -> dropped,
                   # not kept as "splinters" in the final video.
WORKERS_RESERVED = 2  # cores left free for the OS/UI; workers = cpu_count - this


def _default_workers() -> int:
    """Parallel render workers: every available core minus WORKERS_RESERVED."""
    cores = os.cpu_count() or (WORKERS_RESERVED + 1)
    return max(1, cores - WORKERS_RESERVED)


def _audio_duration(path: Path) -> float | None:
    return probe_duration_or_none(path, timeout=60)


def detect_silences(
    audio_path: Path,
    threshold_db: float = THRESHOLD_DB,
    min_duration_s: float = MIN_DETECT_S,
) -> list[tuple[float, float]]:
    """Return [(start, end), ...] silence regions via ffmpeg silencedetect."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(audio_path),
        "-af", f"silencedetect=n={threshold_db}dB:d={min_duration_s}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
    out: list[tuple[float, float]] = []
    cur: float | None = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[0-9.]+)", line)
        if m and cur is not None:
            out.append((cur, max(0.0, float(m.group(1)))))
            cur = None
    return out


def _merge_short_gaps(
    sils: list[tuple[float, float]], min_sound_s: float = MIN_SOUND_S
) -> list[tuple[float, float]]:
    """Merge silences separated by an audible gap shorter than ``min_sound_s``.

    A short audible region sandwiched between two silences is a microsound
    (click, breath, mouth noise), not real speech. Merging the surrounding
    silences across it makes the de-silencer remove the microsound instead of
    leaving a splinter in the final video; only sounds longer than
    ``min_sound_s`` survive as kept content.
    """
    if not sils:
        return sils
    ordered = sorted(sils)
    merged: list[tuple[float, float]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = merged[-1]
        if s - pe < min_sound_s:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _compute_keeps(
    sils: list[tuple[float, float]], dur: float, cap_s: float, pad_s: float = PAD_S,
    lead_in_s: float = 0.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Return (keep_intervals, removed_intervals) on the original timeline.

    For each silence longer than `cap_s` (+ `pad_s`) we remove
    [s + cap_s, e - pad_s]: the leading `cap_s` keeps the pause AFTER the
    previous word (the natural decay/breath, with the word's quiet "s" tail
    intact) and the trailing `pad_s` keeps only a tight lead-in before the next
    word so it starts crisp (render fades it in so it does not pop).

    When `lead_in_s` > 0 (comment format) the silence that touches the start
    of the clip (s ~ 0) is capped to `lead_in_s` instead of `cap_s`, preserving
    that much leading silence so the comment card + AI voiceover have a window
    to play over. Every interior silence is still capped to `cap_s`.
    """
    removed: list[tuple[float, float]] = []
    for s, e in sils:
        eff_cap = lead_in_s if (lead_in_s > 0.0 and s <= 0.25) else cap_s
        if (e - s) > eff_cap + pad_s + 1e-3:
            rs, re_ = s + eff_cap, e - pad_s
            if re_ > rs:
                removed.append((rs, re_))
    if not removed:
        return [], []
    removed.sort()
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for rs, re_ in removed:
        if rs > cursor:
            keeps.append((cursor, rs))
        cursor = max(cursor, re_)
    if cursor < dur:
        keeps.append((cursor, dur))
    return keeps, removed


def _hardlink_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        import shutil

        shutil.copy2(src, dst)


def _filter_script_lines(
    keeps: list[tuple[float, float]], base: float, fade_s: float
) -> list[str]:
    """trim/atrim/concat filter graph for `keeps`, rebased to `base` seconds.

    `base` is subtracted from every timestamp so the same keep list works both
    against the full input (base=0) and against an input opened with
    `-ss base` (whose timeline starts at ~0).
    """
    lines: list[str] = []
    labels: list[str] = []
    for i, (s, e) in enumerate(keeps):
        s_r, e_r = s - base, e - base
        lines.append(f"[0:v]trim=start={s_r:.6f}:end={e_r:.6f},setpts=PTS-STARTPTS[v{i}]")
        afilt = f"atrim=start={s_r:.6f}:end={e_r:.6f},asetpts=PTS-STARTPTS"
        seg = e - s
        if fade_s > 0 and seg > 2 * fade_s:
            afilt += (f",afade=t=in:st=0:d={fade_s:.4f}"
                      f",afade=t=out:st={seg - fade_s:.6f}:d={fade_s:.4f}")
        lines.append(f"[0:a]{afilt}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    lines.append("".join(labels) + f"concat=n={len(keeps)}:v=1:a=1[outv][outa]")
    return lines


def _encode_segments(
    raw_mp4: Path,
    dst_mp4: Path,
    keeps: list[tuple[float, float]],
    fade_s: float,
    *,
    threads: int,
    dst_wav: Path | None = None,
) -> None:
    """Encode the concatenation of `keeps` from raw_mp4.

    With `dst_wav=None` (single-chunk mode) it writes one finished MP4 with
    AAC audio and +faststart -- byte-for-byte the old monolithic behavior.
    With `dst_wav` set (parallel-chunk mode) it writes the chunk VIDEO to
    `dst_mp4` and the chunk AUDIO to `dst_wav` as PCM: AAC pads every encode
    to a frame boundary, and that padding -- a few ms per chunk -- survives
    the concat join and accumulates as audible A/V drift across seams. PCM
    has no encoder padding (the join pads it with real silence only up to
    the shared seam offset) and AAC is encoded ONCE at the join, like the
    old single-encode path.

    Seeks the input to the first keep (`-ss`) and limits the read window
    (`-t`), so a chunk only decodes its own span of the source. The read
    window is padded by one second: with B-frames, packets needed to decode
    the last presented frames of the span can sit past it in decode order,
    and the pad keeps those frames (the trim filters drop the excess).
    `threads` limits BOTH the decoder and the encoder so parallel chunk
    workers do not oversubscribe the machine (0 = ffmpeg auto).
    """
    base = keeps[0][0]
    span = keeps[-1][1] - base
    script = dst_mp4.with_suffix(".filter.txt")
    script.write_text(
        ";\n".join(_filter_script_lines(keeps, base, fade_s)), encoding="utf-8"
    )
    # CRF 16 on this INTERMEDIATE working video: high quality is kept across the
    # later cut + assembly re-encodes (3 generations) without the ~140 Mbps blowup
    # the old CRF 1 produced (a multi-minute source ballooned to ~2 GB here).
    x264 = [
        "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
    ]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-threads", str(threads),
        "-ss", f"{base:.6f}", "-t", f"{span + 1.0:.6f}",
        "-i", str(raw_mp4),
        "-filter_complex_script", str(script),
    ]
    if dst_wav is None:
        cmd += [
            "-map", "[outv]", "-map", "[outa]",
            *x264,
            "-c:a", "aac", "-b:a", "192k",
            "-threads", str(threads),
            "-movflags", "+faststart",
            str(dst_mp4),
        ]
    else:
        cmd += [
            "-map", "[outv]", *x264, "-an", "-threads", str(threads), str(dst_mp4),
            "-map", "[outa]", "-c:a", "pcm_s16le", str(dst_wav),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
    wav_missing = dst_wav is not None and not dst_wav.exists()
    if proc.returncode != 0 or not dst_mp4.exists() or wav_missing:
        # Keep the filter script next to the outputs on FAILURE so the exact
        # ffmpeg invocation can be reproduced by hand while debugging.
        raise RuntimeError(
            f"desilence segment encode failed (exit {proc.returncode}): {proc.stderr[-800:]}"
        )
    try:
        script.unlink()
    except OSError:
        pass


def _chunk_keeps(
    keeps: list[tuple[float, float]], n_chunks: int
) -> list[list[tuple[float, float]]]:
    """Split `keeps` into <= n_chunks contiguous groups balanced by source span.

    Balancing by SOURCE span (not kept duration) spreads the decode work: a
    chunk must decode its whole span, silences included, before trim drops
    them. Keeps are sorted, so grouping by span position preserves order.
    """
    if n_chunks <= 1 or len(keeps) <= 1:
        return [list(keeps)]
    src_start = keeps[0][0]
    span = keeps[-1][1] - src_start
    if span <= 0:
        return [list(keeps)]
    n = min(n_chunks, len(keeps))
    groups: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    for s, e in keeps:
        idx = min(n - 1, int((s - src_start) * n / span))
        groups[idx].append((s, e))
    return [g for g in groups if g]


def _video_span(path: Path) -> float | None:
    """Exact content span of the video stream: last packet pts + duration.

    The MP4 *container* duration of an x264 chunk can under-report the real
    frame span (B-frame edit lists), and the concat demuxer offsets the next
    file by that reported duration -- the error accumulates across seams as
    real A/V drift. Measuring the last packet gives the true span.
    """
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,duration_time",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, creationflags=_NO_WIN,
    )
    if proc.returncode != 0:
        return None
    span = None
    for line in proc.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            try:
                end = float(parts[0]) + float(parts[1])
            except ValueError:
                continue
            span = end if span is None else max(span, end)
    return span


def _write_concat_list(
    paths: list[Path], list_file: Path, durations: list[float | None] | None = None
) -> None:
    """Concat-demuxer list; explicit `duration` lines make offsets exact."""
    lines: list[str] = []
    for i, p in enumerate(paths):
        lines.append("file '{}'".format(p.resolve().as_posix().replace("'", "'\\''")))
        if durations is not None and durations[i] is not None:
            lines.append(f"duration {durations[i]:.6f}")
    list_file.write_text("\n".join(lines), encoding="utf-8")


def _concat_chunk_files(
    chunk_videos: list[Path], chunk_wavs: list[Path], out_mp4: Path
) -> None:
    """Join encoded chunks into out_mp4 via the concat demuxer.

    Video is stream-copied (no second generation loss, no re-encode time).
    Audio comes from the chunks' PCM WAVs -- padded with real silence up to
    each chunk's shared seam offset, so the concatenation is gapless by
    construction -- and is encoded to AAC exactly once here, like the old
    monolithic single-encode path.
    """
    tmp_dir = chunk_videos[0].parent
    video_list = tmp_dir / "_concat_video.txt"
    audio_list = tmp_dir / "_concat_audio.txt"
    # One SHARED offset per chunk for both streams. Inside a chunk the concat
    # filter advances the timeline per segment to the max of the two streams;
    # the seam must do the same, or video (offset by video spans) and audio
    # (offset by audio spans) drift apart by the per-chunk difference.
    spans: list[float | None] = []
    for i, (video, wav) in enumerate(zip(chunk_videos, chunk_wavs)):
        v_span = _video_span(video)
        a_span = probe_duration_or_none(wav, timeout=60)
        span = max(v_span or 0.0, a_span or 0.0) or None
        spans.append(span)
        # Pad the chunk audio with REAL silence up to the shared seam span.
        # A bare pts gap is not silence: the AAC encoder at the join (and any
        # flat PCM decode, like the audio extraction for transcription)
        # collapses it, so the audio would slide earlier at every seam where
        # video outruns audio -- and max() never lets that error cancel. The
        # old monolithic concat filter inserted real silence here; do the
        # same. (Video keeps a <=1-frame held-frame pts gap at audio-longer
        # seams, the same artifact the monolithic graph produced.)
        if (
            span is not None and a_span is not None
            and i < len(chunk_wavs) - 1 and span - a_span > 1e-4
        ):
            padded = wav.with_suffix(".pad.wav")
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
                    "-i", str(wav), "-af", f"apad=whole_dur={span:.6f}",
                    "-c:a", "pcm_s16le", str(padded),
                ],
                capture_output=True, text=True, creationflags=_NO_WIN,
            )
            if proc.returncode != 0 or not padded.exists():
                raise RuntimeError(
                    f"desilence chunk audio pad failed (exit {proc.returncode}): "
                    f"{proc.stderr[-400:]}"
                )
            padded.replace(wav)
    _write_concat_list(chunk_videos, video_list, spans)
    _write_concat_list(chunk_wavs, audio_list, spans)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(video_list),
        "-f", "concat", "-safe", "0", "-i", str(audio_list),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WIN)
    if proc.returncode != 0 or not out_mp4.exists():
        raise RuntimeError(
            f"desilence concat failed (exit {proc.returncode}): {proc.stderr[-800:]}"
        )


def _render_keep_concat(
    raw_mp4: Path,
    out_mp4: Path,
    keeps: list[tuple[float, float]],
    fade_s: float = FADE_S,
    workers: int | None = None,
) -> None:
    """Render out_mp4 = concatenation of the keep intervals (video + audio).

    The keep list is split into contiguous chunks, each chunk is encoded by
    its own single-threaded ffmpeg process running in a worker pool (all CPU
    cores minus WORKERS_RESERVED by default), and the chunks are joined with
    the concat demuxer. A single monolithic filter graph -- the old approach
    -- serializes thousands of trim branches through ffmpeg's single-threaded
    filter scheduler, which turned 90-minute webinars into multi-hour
    one-core renders; chunking keeps every core busy instead.

    A short `fade_s` audio fade-in/out is applied at the edges of every
    segment so the joins are smooth -- no click/"pop" from cutting the
    waveform at a non-zero-crossing point. Filter graphs are passed as
    *script files* so an arbitrary number of segments (thousands of cuts on
    a long video) cannot overflow the command line.
    """
    if workers is None:
        workers = _default_workers()
    workers = max(1, workers)
    # Render to a temp name and promote atomically: the orchestrator resumes
    # any nonzero-size desilenced.mp4, so a partial file left by a crash or a
    # kill mid-encode (or mid +faststart rewrite) must never sit at out_mp4.
    part = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    chunk_groups = _chunk_keeps(keeps, min(len(keeps), workers))
    if len(chunk_groups) <= 1:
        # One worker or one segment: encode straight to the output (ffmpeg
        # auto-threads the single encode; no join step needed).
        _encode_segments(raw_mp4, part, keeps, fade_s, threads=0)
        os.replace(part, out_mp4)
        return
    tmp_dir = out_mp4.parent / "_desilence_chunks"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    chunk_videos = [tmp_dir / f"chunk_{i:04d}.mp4" for i in range(len(chunk_groups))]
    chunk_wavs = [tmp_dir / f"chunk_{i:04d}.wav" for i in range(len(chunk_groups))]
    print(
        f"desilence: rendering {len(keeps)} kept segment(s) in {len(chunk_groups)} "
        f"parallel chunk(s) on {workers} worker(s)",
        flush=True,
    )
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _encode_segments, raw_mp4, video, group, fade_s,
                threads=1, dst_wav=wav,
            )
            for video, wav, group in zip(chunk_videos, chunk_wavs, chunk_groups)
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001 - aggregate, fail once at the end
                errors.append(str(exc))
    if errors:
        # tmp_dir is left in place on failure so the chunk that broke (and its
        # filter script) can be inspected and reproduced by hand.
        raise RuntimeError(
            f"desilence chunk render failed ({len(errors)} of {len(chunk_groups)} "
            "chunk(s)): " + " | ".join(errors[:3])
        )
    _concat_chunk_files(chunk_videos, chunk_wavs, part)
    os.replace(part, out_mp4)
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_keeps_json(keeps_out: Path, keeps: list[tuple[float, float]]) -> None:
    """Write the retained keep-list to `keeps_out` as JSON `[[start, end], ...]`.

    Each pair is a contiguous span of the ORIGINAL `raw_mp4` timeline that
    survives into `out_mp4`, in order. Downstream timeline mapping (e.g. the
    ranking plan) uses this list to translate raw-time stamps into the
    de-silenced `out_mp4` timeline. A full-pass copy (nothing capped) is written
    as the single span `[[0.0, dur]]` so the file always describes the kept
    timeline that produced `out_mp4`.
    """
    keeps_out.parent.mkdir(parents=True, exist_ok=True)
    keeps_out.write_text(
        json.dumps([[round(s, 6), round(e, 6)] for s, e in keeps]),
        encoding="utf-8",
    )


def desilence_video(
    raw_mp4: Path,
    out_mp4: Path,
    *,
    cap_s: float = CAP_S,
    threshold_db: float = THRESHOLD_DB,
    min_detect_s: float = MIN_DETECT_S,
    workers: int | None = None,
    lead_in_s: float = 0.0,
    keeps_out: Path | None = None,
    return_removed: bool = False,
) -> tuple[Path, int, float] | tuple[Path, int, float, list[tuple[float, float]]]:
    """Write `out_mp4` = `raw_mp4` with long silences capped to `cap_s`.

    Returns (path_written, n_capped, removed_seconds). When there is nothing to
    cap (or detection cannot run) it hardlinks the original to `out_mp4` and
    returns (out_mp4, 0, 0.0) so the pipeline still runs on a single canonical
    working video. Raises on an actual ffmpeg render failure.

    The three trailing keyword-only parameters are purely additive — with their
    defaults, behavior and the returned 3-tuple are identical to before, so
    existing callers are unaffected:

    - `lead_in_s` > 0 (comment format) caps the silence at the very START of the
      clip to `lead_in_s` instead of `cap_s`, preserving that much leading
      silence for the comment card + AI voiceover window.
    - `keeps_out` (ranking format) writes the retained keep-list
      `[[start, end], ...]` on the original `raw_mp4` timeline there as JSON.
    - `return_removed=True` returns a 4-tuple (path_written, n_capped,
      removed_seconds, removed_intervals) where removed_intervals are the
      [start, end] spans (on the INPUT timeline) that were cut; feed them to
      `remap_transcript_times` to move a transcript onto the de-silenced
      timeline.
    """

    def _result(
        path: Path, n: int, removed_s: float, removed: list[tuple[float, float]]
    ):
        if return_removed:
            return path, n, removed_s, removed
        return path, n, removed_s

    if os.environ.get("BIONICO_DISABLE_DESILENCE") == "1":
        _hardlink_or_copy(raw_mp4, out_mp4)
        if keeps_out is not None:
            dur = _audio_duration(raw_mp4)
            _write_keeps_json(keeps_out, [(0.0, dur)] if dur and dur > 0 else [])
        return _result(out_mp4, 0, 0.0, [])
    dur = _audio_duration(raw_mp4)
    if dur is None or dur <= 0:
        _hardlink_or_copy(raw_mp4, out_mp4)
        if keeps_out is not None:
            _write_keeps_json(keeps_out, [])
        return _result(out_mp4, 0, 0.0, [])
    try:
        sils = detect_silences(raw_mp4, threshold_db, min_detect_s)
    except Exception:  # noqa: BLE001
        _hardlink_or_copy(raw_mp4, out_mp4)
        if keeps_out is not None:
            _write_keeps_json(keeps_out, [(0.0, dur)])
        return _result(out_mp4, 0, 0.0, [])
    # Drop sub-100ms microsounds wedged between silences (otherwise they survive
    # as splinters): merge the silences around any audible gap < MIN_SOUND_S.
    sils = _merge_short_gaps(sils)
    keeps, removed = _compute_keeps(sils, dur, cap_s, lead_in_s=lead_in_s)
    if not keeps:
        _hardlink_or_copy(raw_mp4, out_mp4)
        if keeps_out is not None:
            _write_keeps_json(keeps_out, [(0.0, dur)])
        return _result(out_mp4, 0, 0.0, [])
    _render_keep_concat(raw_mp4, out_mp4, keeps, workers=workers)
    # The de-silence re-encode auto-rotates the pixels upright but, like the cut,
    # leaves the source's -90 display matrix on the output; strip it so the
    # working video is a true upright 1080x1920 (no double-rotation downstream).
    strip_rotation_metadata(out_mp4)
    if keeps_out is not None:
        _write_keeps_json(keeps_out, keeps)
    removed_s = sum(re_ - rs for rs, re_ in removed)
    return _result(out_mp4, len(removed), removed_s, removed)


def remap_transcript_times(
    transcript_path: Path,
    out_path: Path,
    removed: list[tuple[float, float]],
    offset_s: float = 0.0,
) -> int:
    """Move a transcript's word/spacing times onto the de-silenced timeline.

    The transcript times are on the pre-de-silence body timeline. `offset_s` is
    how far that body was pushed right when something (the comment intro) was
    concatenated before it, so a token at t sits at t + offset_s on the composite
    the de-silencer measured. `removed` are the [start, end] spans the de-silencer
    cut from that composite (`desilence_video(..., return_removed=True)`). A
    composite time T maps to T minus the removed time before it; a token whose
    tail falls inside a cut span is clamped to the cut point. `transcript_path`
    and `out_path` may be the same file. Returns the number of tokens whose start
    or end moved.
    """
    data = json.loads(transcript_path.read_text(encoding="utf-8-sig"))
    rem = sorted((float(s), float(e)) for s, e in removed if e > s)

    def _shift(t: float) -> float:
        t += offset_s
        gone = 0.0
        for rs, re_ in rem:
            if re_ <= t:           # cut span fully before t -> all of it is gone
                gone += re_ - rs
            elif rs < t:           # t sits inside this cut span -> clamp to its head
                gone += t - rs
                break
            else:                  # cut span (and every later one) is after t
                break
        return max(0.0, t - gone)

    changed = 0
    for tok in data.get("words", []):
        if "start" not in tok or "end" not in tok:
            continue
        ns = round(_shift(float(tok["start"])), 3)
        ne = round(_shift(float(tok["end"])), 3)
        if ne < ns:
            ne = ns
        if ns != tok.get("start") or ne != tok.get("end"):
            changed += 1
        tok["start"], tok["end"] = ns, ne

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cap long silences in a video up front.")
    parser.add_argument("raw_mp4", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--cap-ms", type=float, default=CAP_S * 1000.0)
    parser.add_argument("--threshold-db", type=float, default=THRESHOLD_DB)
    parser.add_argument("--min-detect-ms", type=float, default=MIN_DETECT_S * 1000.0)
    parser.add_argument(
        "--workers", type=int, default=None,
        help="parallel encode workers (default: all CPU cores minus 2)",
    )
    parser.add_argument(
        "--lead-in-ms", type=float, default=0.0,
        help="comment format: preserve this much leading silence (the AI voiceover window)",
    )
    args = parser.parse_args(argv)

    if not args.raw_mp4.exists():
        print(f"error: input not found: {args.raw_mp4}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _, n_capped, removed_s = desilence_video(
            args.raw_mp4,
            args.output,
            cap_s=args.cap_ms / 1000.0,
            threshold_db=args.threshold_db,
            min_detect_s=args.min_detect_ms / 1000.0,
            workers=args.workers,
            lead_in_s=args.lead_in_ms / 1000.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"desilence: FAILED: {exc}", file=sys.stderr)
        return 1
    if n_capped:
        print(
            f"desilence: capped {n_capped} silence(s) >{args.cap_ms:.0f}ms on the VIDEO "
            f"(removed {removed_s:.1f}s of dead air); the whole pipeline now runs on "
            f"{args.output.name}, timelines native.",
            flush=True,
        )
    else:
        print(
            f"desilence: no silence >{args.cap_ms:.0f}ms found; using source as-is "
            f"({args.output.name}).",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
