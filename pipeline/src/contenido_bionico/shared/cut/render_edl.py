"""Deterministic render of an EDL into `source.mp4` (cut talking-head).

Reads `_intermediates/edl_render.json` (a list of keep `ranges` with
start/end seconds over the raw MP4) and produces `<run>/source.mp4`: the
talking-head video cut and concatenated, ready for PHASE 2 (animate) to
overlay animations on top via `animate/helpers/assembly_render.py`.

This is the last deterministic step of PHASE 1 (cut). It is invoked by
`orchestrator.py` after `make_render_edl.py`.

Usage:
    python render_edl.py <edl_render.json> <raw.mp4> -o <source.mp4>
    python render_edl.py <edl_render.json> <raw.mp4> -o <source.mp4> --max-seconds 30
    python render_edl.py <edl_render.json> <raw.mp4> -o <source.mp4> --parallel-chunks 6

Implementation:
    Builds a filter_complex with trim+atrim+setpts+asetpts per range and a
    concat at the end. ffmpeg's `filter_complex` is single-threaded for
    one graph; for long sources (>20 min by default, or any value via
    `--parallel-chunks`), the helper splits the EDL into N time-disjoint
    chunks, seeks each chunk to its local source window, renders each one
    in parallel with its own ffmpeg + filter graph, then concat-demuxes
    the N intermediate MP4s with `-c copy` (no re-encode). Codec/container
    are calibrated so the merged output
    matches what a single-pass render would produce; assembly_render can
    consume the output directly without re-encoding (H.264 yuv420p + AAC).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# This file is also run by path as a child process (see shared/cut/orchestrator),
# so make the package importable before the package imports below.
_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# Hide the console window when spawning subprocesses on Windows. No-op elsewhere.
from contenido_bionico.shared.ffmpeg import (  # noqa: E402
    NO_WINDOW as _NO_WIN,
    probe_duration_or_none,
    strip_rotation_metadata,
)

OUT_FPS = 30

# Mandatory breathing room at the very end of EVERY cut: hold the last frame and
# pad silence for TAIL_PAD_S after the last spoken word so the video never
# hard-stops mid-word / feels chopped. Baked into source.mp4 here, so it
# propagates to the final reel, the voice atom, and every derived format video.
TAIL_PAD_S = 0.5

# Source duration above which `--parallel-chunks auto` enables chunking.
_AUTO_CHUNK_THRESHOLD_S = 20 * 60  # 20 minutes
# Target maximum source-time span per chunk in auto mode. ~20 min keeps each
# chunk's filter graph within ffmpeg's sweet spot.
_AUTO_CHUNK_TARGET_S = 20 * 60
_AUTO_CHUNK_MAX = 6
_AUTO_RANGE_THRESHOLD = 500
_AUTO_TARGET_RANGES = 250
_CHUNK_INPUT_TAIL_PAD_S = 0.5


def select_ranges(ranges: list[dict], max_seconds: float | None) -> list[dict]:
    """Return the ranges that cover the first `max_seconds` of the final MP4.

    If max_seconds is None, returns all ranges untouched.
    If the last included range exceeds the limit, its `end` is trimmed.
    """
    if max_seconds is None:
        return list(ranges)

    selected: list[dict] = []
    cum = 0.0
    for r in ranges:
        r_start = float(r["start"])
        r_end = float(r["end"])
        r_dur = r_end - r_start
        if cum + r_dur <= max_seconds:
            selected.append({"source": r["source"], "start": r_start, "end": r_end})
            cum += r_dur
            if cum >= max_seconds:
                break
        else:
            remaining = max_seconds - cum
            if remaining > 0:
                selected.append(
                    {"source": r["source"], "start": r_start, "end": r_start + remaining}
                )
            break
    return selected


def build_ffmpeg_command(
    source_path: Path,
    ranges: list[dict],
    output_path: Path,
    *,
    input_seek_s: float = 0.0,
    input_duration_s: float | None = None,
    out_fps: int = OUT_FPS,
    tail_s: float = 0.0,
) -> list[str]:
    """Build the ffmpeg command with filter_complex + concat.

    When `tail_s > 0`, the concatenated output is extended by `tail_s` seconds:
    the last video frame is cloned (tpad) and the audio is padded with silence
    (apad). Callers pass this only for the FINAL output (single-pass render or
    the last chunk of a parallel render) so exactly one tail is appended.
    """
    n = len(ranges)
    filter_parts: list[str] = []
    video_inputs: list[str] = []
    audio_inputs: list[str] = []
    for i, r in enumerate(ranges):
        s = float(r["start"])
        e = float(r["end"])
        # Short fade in/out at each cut edge so the concat joins are smooth: no
        # click/"pop" from joining the waveform at a non-zero-crossing point.
        seg = e - s
        afade = (
            f",afade=t=in:st=0:d=0.008,afade=t=out:st={seg - 0.008:.6f}:d=0.008"
            if seg > 0.016 else ""
        )
        filter_parts.append(
            f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS{afade}[a{i}]"
        )
        video_inputs.append(f"[v{i}]")
        audio_inputs.append(f"[a{i}]")
    if tail_s > 0:
        vconcat = "".join(video_inputs) + f"concat=n={n}:v=1:a=0[catv]"
        aconcat = (
            "".join(audio_inputs)
            + f"concat=n={n}:v=0:a=1[cata];[cata]apad=pad_dur={tail_s:.3f}[outa]"
        )
        fps = (
            f"[catv]fps={out_fps},"
            f"tpad=stop_mode=clone:stop_duration={tail_s:.3f}[outv]"
        )
    else:
        vconcat = "".join(video_inputs) + f"concat=n={n}:v=1:a=0[catv]"
        aconcat = "".join(audio_inputs) + f"concat=n={n}:v=0:a=1[outa]"
        fps = f"[catv]fps={out_fps}[outv]"
    filter_complex = ";".join(filter_parts) + ";" + vconcat + ";" + aconcat + ";" + fps

    input_args: list[str] = []
    if input_seek_s > 0:
        input_args += ["-ss", f"{input_seek_s:.6f}"]
    if input_duration_s is not None and input_duration_s > 0:
        input_args += ["-t", f"{input_duration_s:.6f}"]

    return [
        "ffmpeg",
        "-y",
        *input_args,
        "-i",
        str(source_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        # CRF 16: intermediate cut output (was 1 -- near-lossless, multi-GB).
        "-crf",
        "16",
        "-g",
        str(out_fps),
        "-keyint_min",
        str(out_fps),
        "-force_key_frames",
        "expr:gte(t,n_forced)",
        "-c:a",
        "aac",
        # 320k: the cut output is an INTERMEDIATE that later stages re-encode,
        # so the extra bitrate is cheap insurance against generation loss.
        "-b:a",
        "320k",
        "-r",
        str(out_fps),
        str(output_path),
    ]


def _source_duration_s(path: Path) -> float | None:
    """Best-effort ffprobe of the source duration in seconds. None on
    failure — the caller falls back to a single-chunk render."""
    return probe_duration_or_none(path, timeout=30)


def _decide_chunk_count(
    requested: str | int,
    source_duration_s: float | None,
    range_count: int | None = None,
) -> int:
    """Resolve `--parallel-chunks` argument to a concrete N.

    `auto` (default):
      - 1 if the source is short and the EDL is small.
      - Otherwise the larger of source-duration chunks and range-count
        chunks, capped at `_AUTO_CHUNK_MAX`.
    An integer string or int is taken as the literal count (min 1).
    """
    if isinstance(requested, str) and requested.lower() == "auto":
        duration_chunks = 1
        if source_duration_s is not None and source_duration_s >= _AUTO_CHUNK_THRESHOLD_S:
            duration_chunks = math.ceil(source_duration_s / _AUTO_CHUNK_TARGET_S)
        range_chunks = 1
        if range_count is not None and range_count >= _AUTO_RANGE_THRESHOLD:
            range_chunks = math.ceil(range_count / _AUTO_TARGET_RANGES)
        return min(_AUTO_CHUNK_MAX, max(1, duration_chunks, range_chunks))
    try:
        n = int(requested)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def _split_ranges_into_chunks(
    ranges: list[dict], n_chunks: int
) -> list[list[dict]]:
    """Partition `ranges` into `n_chunks` non-overlapping groups by
    source time order, never splitting an individual range.

    Boundaries are chosen so each chunk holds a roughly equal share of
    keep-duration (not equal number of ranges). The mapper output is
    already in source-time order, so we just walk it accumulating keep
    duration and start a new chunk when the per-chunk budget is reached.
    Returns a list of length `n_chunks`; some entries may be empty if
    the EDL has fewer ranges than chunks (caller treats empty chunks as
    no-ops).
    """
    if n_chunks <= 1 or len(ranges) <= 1:
        return [list(ranges)]
    total_keep = sum(float(r["end"]) - float(r["start"]) for r in ranges)
    target_per_chunk = total_keep / n_chunks
    chunks: list[list[dict]] = [[] for _ in range(n_chunks)]
    cursor = 0
    accum = 0.0
    for r in ranges:
        dur = float(r["end"]) - float(r["start"])
        chunks[cursor].append(r)
        accum += dur
        # Move to next chunk once we've crossed the target — only if we
        # haven't already filled every slot (the last chunk gets the
        # remainder).
        if accum >= target_per_chunk and cursor < n_chunks - 1:
            cursor += 1
            accum = 0.0
    return chunks


def _render_one_chunk(
    args_tuple: tuple[int, Path, list[dict], Path, float]
) -> tuple[int, int, str]:
    """Run ffmpeg for a single chunk. Returns (chunk_idx, returncode, msg).

    Used as the work unit for `concurrent.futures` so we can render
    multiple chunks in parallel without re-implementing process tracking.
    Captures stderr to surface the actual ffmpeg error in the parent
    process; capturing stdout too keeps the parent log clean. `tail_s` is
    non-zero only for the last chunk so exactly one end-pad is appended.
    """
    idx, source_path, chunk_ranges, output_path, tail_s = args_tuple
    seek_start, relative_ranges, input_duration = _relative_chunk_ranges(chunk_ranges)
    cmd = build_ffmpeg_command(
        source_path,
        relative_ranges,
        output_path,
        input_seek_s=seek_start,
        input_duration_s=input_duration,
        tail_s=tail_s,
    )
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WIN,
    )
    msg = proc.stderr or proc.stdout or ""
    return idx, proc.returncode, msg.strip()[-2000:]  # tail of stderr


def _relative_chunk_ranges(chunk_ranges: list[dict]) -> tuple[float, list[dict], float]:
    """Convert absolute source ranges into a local seek window for one chunk."""
    if not chunk_ranges:
        raise ValueError("chunk_ranges must not be empty")
    seek_start = float(chunk_ranges[0]["start"])
    last_end = float(chunk_ranges[-1]["end"])
    relative: list[dict] = []
    for r in chunk_ranges:
        relative.append(
            {
                "source": r.get("source", ""),
                "start": float(r["start"]) - seek_start,
                "end": float(r["end"]) - seek_start,
            }
        )
    input_duration = max(0.0, last_end - seek_start + _CHUNK_INPUT_TAIL_PAD_S)
    return seek_start, relative, input_duration


def _tmp_output_path(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}.mp4")


def _commit_output(tmp_output: Path, final_output: Path) -> None:
    os.replace(tmp_output, final_output)
    # Phone clips carry a -90 display matrix; the auto-rotate during this render
    # bakes the pixels upright but leaves the tag, double-rotating playback to
    # sideways. Strip it so the cut output is a true upright 1080x1920.
    strip_rotation_metadata(final_output)


def _concat_chunks(part_paths: list[Path], output_path: Path) -> int:
    """Concatenate the per-chunk MP4s into the final `source.mp4` via
    ffmpeg's concat demuxer with `-c copy` (stream copy, no re-encode).

    Stream copy works because every chunk was rendered with the same
    `build_ffmpeg_command` settings — identical codec, profile, level,
    pixel format, and timebase. Keyframes were force-emitted at every
    second via `-force_key_frames`, so chunk boundaries land on a clean
    IDR and the demuxer can splice without artifacts.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fp:
        list_path = Path(fp.name)
        for p in part_paths:
            # `file` directive must use POSIX-style absolute paths even on
            # Windows; subprocess will handle the rest. Single quotes inside
            # the path must be escaped per the concat-demuxer rule (close
            # the quote, emit an escaped quote, reopen: ' -> '\'') or a
            # path with an apostrophe breaks the final concat.
            escaped = p.as_posix().replace("'", "'\\''")
            fp.write(f"file '{escaped}'\n")
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WIN
        )
        if result.returncode != 0:
            print(
                "ERROR: concat demuxer failed:\n" + (result.stderr or ""),
                file=sys.stderr,
            )
        return result.returncode
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Render EDL into source.mp4 (cut talking head).")
    ap.add_argument("edl_json", type=Path, help="edl_render.json produced by make_render_edl.py")
    ap.add_argument("source_mp4", type=Path, help="original raw MP4 (input to the cut)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="destination path, typically <run>/source.mp4")
    ap.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="If set, render only the first N seconds of the output",
    )
    ap.add_argument(
        "--parallel-chunks",
        type=str,
        default="auto",
        help=(
            "How many parallel ffmpeg processes to use. 'auto' (default) "
            "splits long sources or high-range EDLs into up to 6 chunks; "
            "1 forces the legacy single-pass path; any int overrides the "
            "auto policy. Each chunk seeks to its local source window, and "
            "the per-chunk intermediates are concatenated with `-c copy` "
            "(no re-encode)."
        ),
    )
    args = ap.parse_args()

    if not args.source_mp4.exists():
        print(f"ERROR: source does not exist: {args.source_mp4}", file=sys.stderr)
        return 1
    if not args.edl_json.exists():
        print(f"ERROR: edl does not exist: {args.edl_json}", file=sys.stderr)
        return 1

    try:
        edl = json.loads(args.edl_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: edl is not valid JSON ({args.edl_json}): {exc}", file=sys.stderr)
        return 1

    ranges = edl.get("ranges")
    if not isinstance(ranges, list):
        print(f"ERROR: edl has no 'ranges' key (list): {args.edl_json}", file=sys.stderr)
        return 1
    if not ranges:
        print(f"ERROR: edl contains no ranges: {args.edl_json}", file=sys.stderr)
        return 1

    # Filter out degenerate ranges (end <= start) that would break filter_complex.
    clean: list[dict] = []
    for i, r in enumerate(ranges):
        try:
            s = float(r["start"])
            e = float(r["end"])
        except (KeyError, TypeError, ValueError):
            print(f"ERROR: range {i} is malformed: {r}", file=sys.stderr)
            return 1
        if e <= s:
            print(f"WARN: discarding range {i} with end<=start: start={s} end={e}", file=sys.stderr)
            continue
        clean.append({"source": r.get("source", ""), "start": s, "end": e})

    if not clean:
        print("ERROR: no valid range left after filtering", file=sys.stderr)
        return 1

    selected = select_ranges(clean, args.max_seconds)
    if not selected:
        print("ERROR: --max-seconds left zero selected ranges", file=sys.stderr)
        return 1

    total = sum(float(r["end"]) - float(r["start"]) for r in selected)
    print(f"total ranges in EDL:   {len(ranges)}")
    print(f"selected ranges:       {len(selected)}")
    print(f"estimated duration:    {total:.2f}s")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    source_duration = _source_duration_s(args.source_mp4)
    n_chunks = _decide_chunk_count(
        args.parallel_chunks,
        source_duration,
        range_count=len(selected),
    )
    if source_duration is not None:
        print(
            f"source duration:       {source_duration:.1f}s "
            f"({source_duration/60:.1f} min)"
        )
    print(
        f"parallel chunks:       {n_chunks} "
        f"(requested={args.parallel_chunks!r})"
    )
    print(f"output fps:            {OUT_FPS}")

    tmp_output = _tmp_output_path(args.output)
    try:
        tmp_output.unlink()
    except FileNotFoundError:
        pass

    if n_chunks <= 1:
        # Legacy single-pass path. Same shape as before so anything
        # downstream that scrapes ffmpeg stderr in real time still works.
        cmd = build_ffmpeg_command(args.source_mp4, selected, tmp_output, tail_s=TAIL_PAD_S)
        print(f"running ffmpeg with {len(selected)} ranges (single pass)...")
        result = subprocess.run(cmd, capture_output=False, creationflags=_NO_WIN)
        if result.returncode != 0:
            print(f"ERROR: ffmpeg failed (exit {result.returncode})", file=sys.stderr)
            return result.returncode
        if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
            print(
                f"ERROR: ffmpeg produced no output or it is empty: {tmp_output}",
                file=sys.stderr,
            )
            return 1
        _commit_output(tmp_output, args.output)
        print(f"\ndone: {args.output}")
        return 0

    # Chunked parallel path. Split, render N intermediates in parallel,
    # concat with -c copy.
    chunks = _split_ranges_into_chunks(selected, n_chunks)
    non_empty_chunks = [(i, c) for i, c in enumerate(chunks) if c]
    if not non_empty_chunks:
        print("ERROR: no non-empty chunk after split", file=sys.stderr)
        return 1
    print(
        "chunk plan: "
        + ", ".join(
            f"#{i}={len(c)} ranges, "
            f"{sum(float(r['end'])-float(r['start']) for r in c):.1f}s keep, "
            f"seek={float(c[0]['start']):.1f}s"
            for i, c in non_empty_chunks
        )
    )

    with tempfile.TemporaryDirectory(prefix="render_edl_parts_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        part_paths: list[Path] = []
        work_items: list[tuple[int, Path, list[dict], Path, float]] = []
        last_chunk_idx = non_empty_chunks[-1][0]  # tail-pad only the final chunk
        for chunk_idx, chunk_ranges in non_empty_chunks:
            part_path = tmpdir_path / f"part_{chunk_idx:02d}.mp4"
            part_paths.append(part_path)
            tail_s = TAIL_PAD_S if chunk_idx == last_chunk_idx else 0.0
            work_items.append(
                (chunk_idx, args.source_mp4, chunk_ranges, part_path, tail_s)
            )
        max_workers = min(len(work_items), n_chunks)
        print(
            f"launching {max_workers} parallel ffmpeg process(es)..."
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as executor:
            futures = [
                executor.submit(_render_one_chunk, item) for item in work_items
            ]
            failures: list[tuple[int, int, str]] = []
            for fut in concurrent.futures.as_completed(futures):
                idx, rc, msg = fut.result()
                if rc == 0:
                    print(f"  chunk #{idx}: OK")
                else:
                    failures.append((idx, rc, msg))
                    print(
                        f"  chunk #{idx}: FAILED (exit {rc})\n    {msg}",
                        file=sys.stderr,
                    )
        if failures:
            print(
                f"ERROR: {len(failures)} chunk(s) failed; aborting concat",
                file=sys.stderr,
            )
            return 1
        for p in part_paths:
            if not p.exists() or p.stat().st_size <= 0:
                print(
                    f"ERROR: chunk part missing or empty: {p}",
                    file=sys.stderr,
                )
                return 1
        print(f"concatenating {len(part_paths)} part(s) with -c copy...")
        concat_rc = _concat_chunks(part_paths, tmp_output)
        if concat_rc != 0:
            return concat_rc

    if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
        print(
            f"ERROR: final output missing or empty: {tmp_output}",
            file=sys.stderr,
        )
        return 1
    _commit_output(tmp_output, args.output)
    print(f"\ndone: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
