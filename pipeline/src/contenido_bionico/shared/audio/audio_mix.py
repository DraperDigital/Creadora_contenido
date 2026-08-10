"""Mix background music + per-segment SFX with the existing voice track.

Reads:
    runs/<id>/final.mp4         (video-only target, audio = voice from source.mp4)
    runs/<id>/Audio_Plan.json   (music selection + absolute-time SFX cues)

Writes:
    runs/<id>/final.video_only.mp4   (backup of the original final.mp4)
    runs/<id>/final.mp4              (new audio-mixed final)
    runs/<id>/logs/Audio_Mix_Filter.txt   (the filter graph, for debugging)
    runs/<id>/logs/Audio_Mix_Report.json

Filter graph (full case, music + N sfx over M distinct files):
    [0:a]aresample=48000,(optional cleanup),volume=<voice gain> -> asplit
        -> [voice] + [vsc] (the ducking sidechain copy)
    [1:a] -> asplit + acrossfade-looped, trimmed+faded music -> [music_pre]
    [music_pre][vsc]sidechaincompress -> [music]  (music ducks under speech)
    [2:a]...[M+1:a] one input per distinct SFX file; files used by several
        cues fan out via asplit, then each cue gets adelay+gain -> [s0]...[s{N-1}]
    [s0]..[s{N-1}] amix -> [sfx_bus]
    [voice][music][sfx_bus] amix -> master volume -> alimiter -> [out]

Loudness policy (constants in shared/ffmpeg.py): the voice source is measured
(measure_lufs) and brought to VOICE_TARGET_LUFS with one static gain; music
gain is computed at plan time (build_audio_plan) toward MUSIC_TARGET_LUFS; SFX
gains are computed here toward SFX_TARGET_LUFS from cached measurements; the
master bus gets a static gain toward MASTER_TARGET_LUFS and a true-peak
ceiling of -1 dBTP (alimiter=limit=0.891). Optional voice cleanup (highpass +
denoise + gentle compression) runs when BIONICO_VOICE_CLEANUP=1 (default off).

Idempotency: the mix ALWAYS reads the voice from final.video_only.mp4 once it
exists (see mix() below), so re-running never stacks gain — every pass
re-measures the same untouched voice-only source.

The graph is passed to ffmpeg via -filter_complex_script (a file) so a
40-cue plan never hits the command-line length limit on Windows.

Degrades gracefully:
    - No music in Audio_Plan.json -> skip music input
    - Music track unprobeable -> hard -stream_loop loop instead of crossfade
    - No SFX cues -> skip sfx_bus
    - Neither music nor SFX -> no-op (final.mp4 untouched)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from contenido_bionico.shared.ffmpeg import (
    MASTER_PEAK_LIMIT,
    MASTER_TARGET_LUFS,
    NO_WINDOW as _NO_WIN,
    SFX_TARGET_LUFS,
    VOICE_TARGET_LUFS,
    measure_lufs,
    probe_duration_or_none,
    static_gain_db,
)

from .loudness_cache import cached_lufs

# Upper bound on acrossfade-chained copies of the music track. 128 copies of
# even the shortest bundled long track (~150s) cover > 5 hours of video.
_MAX_MUSIC_LOOP_COPIES = 128

# Optional voice-cleanup chain, enabled by BIONICO_VOICE_CLEANUP=1 (default
# OFF): rumble highpass, light FFT denoise, gentle 2:1 leveling compressor.
_VOICE_CLEANUP_CHAIN = (
    "highpass=f=75,"
    "afftdn=nf=-25,"
    "acompressor=ratio=2:threshold=-18dB:attack=15:release=200:makeup=2dB,"
)

# Music ducking: the music bus is compressed with the voice as the sidechain
# key, so the bed dips ~automatically whenever there is speech and swells back
# in pauses. GENTLE by design: threshold=0.05 (~-26 dBFS) keys only on louder
# speech and ratio=3 dips the bed a few dB instead of crushing it, so the music
# stays audible UNDER continuous talking-head speech (paired with the raised
# MUSIC_TARGET_LUFS). release=400 lets the bed breathe back between phrases.
_DUCK_FILTER = "sidechaincompress=threshold=0.05:ratio=3:attack=50:release=400"


def _voice_cleanup_enabled() -> bool:
    """BIONICO_VOICE_CLEANUP env flag (default off)."""
    raw = (os.environ.get("BIONICO_VOICE_CLEANUP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class AudioMixError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise AudioMixError(f"required file does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AudioMixError(f"{path}: not valid JSON ({exc})") from exc


def _probe_track_duration(path: str) -> float | None:
    """Duration of an audio file in seconds, or None if ffprobe can't say."""
    return probe_duration_or_none(path)


def _music_chain(
    music: dict, music_idx: int, duration: float, out_label: str = "music"
) -> tuple[list[str], list[str]]:
    """Return (input_args, filter_parts) for the background-music chain.

    With the manifest's crossfade_loop strategy the track is decoded once,
    fanned out with asplit, and the copies are chained with acrossfade so the
    loop point is inaudible (N copies cover N*track - (N-1)*crossfade
    seconds). If the track can't be probed, or the strategy/crossfade make a
    crossfade impossible, this degrades to the old hard loop: -stream_loop -1
    on the input + atrim, which never fails but has an audible seam.
    """
    music_file = str(music["file"])
    base_gain = float(music["base_gain_db"])
    fade_in = float(music.get("intro_fade_seconds", 1.5))
    fade_out = float(music.get("outro_fade_seconds", 2.0))
    fade_out_st = max(duration - fade_out, 0.0)
    tail = (
        f"atrim=duration={duration:.4f},"
        f"asetpts=PTS-STARTPTS,"
        f"volume={base_gain}dB,"
        f"afade=t=in:st=0:d={fade_in},"
        f"afade=t=out:st={fade_out_st:.4f}:d={fade_out}"
        f"[{out_label}]"
    )

    loop_strategy = str(music.get("loop_strategy", "crossfade_loop"))
    crossfade = float(music.get("crossfade_seconds", 2.0))
    track_dur = _probe_track_duration(music_file) if loop_strategy == "crossfade_loop" else None
    if track_dur is not None and track_dur > 0:
        crossfade = min(max(crossfade, 0.0), track_dur / 2)
    if track_dur is None or track_dur <= 0 or crossfade < 0.01:
        return (
            ["-stream_loop", "-1", "-i", music_file],
            [f"[{music_idx}:a]aresample=48000," + tail],
        )

    inputs = ["-i", music_file]
    if track_dur >= duration:
        # Track already covers the video; no looping needed.
        return inputs, [f"[{music_idx}:a]aresample=48000," + tail]

    copies = math.ceil((duration - crossfade) / (track_dur - crossfade))
    copies = max(2, min(copies, _MAX_MUSIC_LOOP_COPIES))
    parts = [
        f"[{music_idx}:a]aresample=48000,asplit={copies}"
        + "".join(f"[m{j}]" for j in range(copies))
    ]
    prev = "m0"
    for j in range(1, copies):
        joined = f"mx{j}"
        parts.append(f"[{prev}][m{j}]acrossfade=d={crossfade:.4f}[{joined}]")
        prev = joined
    parts.append(f"[{prev}]" + tail)
    return inputs, parts


def _build_graph(
    plan: dict,
    *,
    voice_gain_db: float = 0.0,
    voice_cleanup: bool = False,
    sfx_gain_by_file: dict[str, float] | None = None,
) -> tuple[list[str], str, str]:
    """Return (extra_input_args, filter_graph, final_label).

    extra_input_args are the additional -i (and -stream_loop) flags appended
    to the ffmpeg command line after the primary video input. filter_graph is
    the full graph text (one chain per line) meant for -filter_complex_script.
    final_label is the label to map onto the output audio stream.

    `voice_gain_db` is the measured static normalization gain for the voice
    chain; `voice_cleanup` prepends the optional cleanup chain; and
    `sfx_gain_by_file` overrides the manifest's flat per-cue gain with a
    measured per-file gain (files missing from the dict keep the cue gain).
    """
    music = plan.get("music")
    sfx_cues = plan.get("sfx_cues") or []
    duration = float(plan["source_duration_seconds"])
    sfx_gains = sfx_gain_by_file or {}
    master_gain_db = round(MASTER_TARGET_LUFS - VOICE_TARGET_LUFS, 2)
    master_tail = (
        f"volume={master_gain_db}dB,"
        f"alimiter=limit={MASTER_PEAK_LIMIT}:level=false"
    )

    has_music = music is not None and bool(music.get("file"))
    cleanup = _VOICE_CLEANUP_CHAIN if voice_cleanup else ""
    voice_head = f"[0:a]aresample=48000,{cleanup}volume={voice_gain_db}dB"
    extra_inputs: list[str] = []
    # With music, the normalized voice is split: one copy feeds the mix, the
    # other keys the music-ducking sidechain compressor.
    parts: list[str] = [
        voice_head + (",asplit=2[voice][vsc]" if has_music else "[voice]")
    ]
    bus_labels: list[str] = ["[voice]"]

    # Music (optional), ducked under the voice.
    next_idx = 1
    if has_music:
        music_inputs, music_parts = _music_chain(
            music, next_idx, duration, out_label="music_pre"
        )
        next_idx += 1
        extra_inputs += music_inputs
        parts += music_parts
        parts.append(f"[music_pre][vsc]{_DUCK_FILTER}[music]")
        bus_labels.append("[music]")

    # SFX cues: one -i per distinct file (a plan may repeat the same handful
    # of files across up to 40 cues per segment); cues sharing a file fan out
    # from a single decoded stream via asplit, then each cue gets its own
    # adelay + gain branch.
    cues_by_file: dict[str, list[tuple[int, dict]]] = {}
    for i, cue in enumerate(sfx_cues):
        file_path = cue.get("file")
        if not file_path:
            continue
        cues_by_file.setdefault(str(file_path), []).append((i, cue))

    labeled_sfx: list[tuple[int, str]] = []
    for file_path, file_cues in cues_by_file.items():
        sfx_idx = next_idx
        next_idx += 1
        extra_inputs += ["-i", file_path]
        if len(file_cues) > 1:
            parts.append(
                f"[{sfx_idx}:a]aresample=48000,asplit={len(file_cues)}"
                + "".join(f"[f{sfx_idx}_{j}]" for j in range(len(file_cues)))
            )
            sources = [(f"[f{sfx_idx}_{j}]", "") for j in range(len(file_cues))]
        else:
            sources = [(f"[{sfx_idx}:a]", "aresample=48000,")]
        measured_gain = sfx_gains.get(file_path)
        for (src, prefix), (i, cue) in zip(sources, file_cues):
            delay_ms = max(int(round(float(cue["absolute_seconds"]) * 1000)), 0)
            gain_db = measured_gain if measured_gain is not None else float(cue["gain_db"])
            label = f"s{i}"
            parts.append(
                f"{src}"
                f"{prefix}"
                f"adelay={delay_ms}:all=1,"
                f"volume={gain_db}dB,"
                f"apad=pad_dur=0.05"
                f"[{label}]"
            )
            labeled_sfx.append((i, f"[{label}]"))

    sfx_labels = [label for _i, label in sorted(labeled_sfx)]

    if sfx_labels:
        parts.append(
            f"{''.join(sfx_labels)}"
            f"amix=inputs={len(sfx_labels)}:normalize=0:dropout_transition=0"
            f"[sfx_bus]"
        )
        bus_labels.append("[sfx_bus]")

    if len(bus_labels) == 1:
        # No music and no SFX resolved — still normalize + master the voice.
        parts.append(f"[voice]{master_tail}[out]")
        return extra_inputs, ";\n".join(parts), "out"

    parts.append(
        f"{''.join(bus_labels)}"
        f"amix=inputs={len(bus_labels)}:normalize=0:dropout_transition=0,"
        f"{master_tail}"
        f"[out]"
    )
    return extra_inputs, ";\n".join(parts), "out"


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        creationflags=_NO_WIN,
    )
    if proc.returncode != 0:
        raise AudioMixError(
            "ffmpeg failed (exit "
            f"{proc.returncode}):\n--- cmd ---\n{' '.join(cmd)}\n"
            f"--- stderr ---\n{proc.stderr}"
        )


def mix(run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    final_mp4 = run_dir / "final.mp4"
    plan_path = run_dir / "Audio_Plan.json"
    if not final_mp4.exists():
        raise AudioMixError(f"final.mp4 not found at {final_mp4}")
    plan = _read_json(plan_path)

    music = plan.get("music")
    cues = plan.get("sfx_cues") or []
    unresolved_cues = plan.get("unresolved_cues") or []
    if music is None and not cues:
        result = {
            "status": "noop",
            "reason": "no music and no SFX cues; final.mp4 left unchanged",
            "final_path": str(final_mp4),
            "unresolved_cues": unresolved_cues,
            "n_unresolved_cues": len(unresolved_cues),
        }
        _write_report(run_dir, result)
        return result

    # Idempotent re-mix: if final.video_only.mp4 already exists from a previous
    # audio-mix run, treat it as the canonical voice-only source. Otherwise
    # final.mp4 (just written by assembly_render) is the voice-only source.
    # Without this, re-running the audio stage would mix music onto already-
    # mixed audio and the music layer would compound on every run. It also
    # keeps the voice normalization idempotent: the gain below is always
    # measured against the SAME untouched voice-only source, never against an
    # already-normalized mix, so it cannot stack across re-runs.
    video_only_backup = run_dir / "final.video_only.mp4"
    source_for_mix = video_only_backup if video_only_backup.exists() else final_mp4

    # Voice normalization: one static gain toward VOICE_TARGET_LUFS, measured
    # on the voice-only source (0.0 when the source has no measurable audio).
    voice_lufs = measure_lufs(source_for_mix)
    voice_gain_db = static_gain_db(voice_lufs, VOICE_TARGET_LUFS)

    # SFX normalization: per-file measured gain toward SFX_TARGET_LUFS, cached
    # in the library's loudness sidecar. Unmeasurable files fall back to the
    # manifest's per-cue default gain inside _build_graph.
    library_root = plan.get("library_root")
    sfx_gain_by_file: dict[str, float] = {}
    for cue in cues:
        file_path = cue.get("file")
        if not file_path or str(file_path) in sfx_gain_by_file:
            continue
        sfx_lufs = cached_lufs(file_path, library_root)
        if sfx_lufs is not None:
            sfx_gain_by_file[str(file_path)] = static_gain_db(sfx_lufs, SFX_TARGET_LUFS)

    extra_inputs, filter_graph, final_label = _build_graph(
        plan,
        voice_gain_db=voice_gain_db,
        voice_cleanup=_voice_cleanup_enabled(),
        sfx_gain_by_file=sfx_gain_by_file,
    )

    # The graph can get long (one chain per cue); ffmpeg reads it from a file
    # via -filter_complex_script so the command line stays short on every OS.
    # The file is kept under logs/ as a debugging artifact.
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    filter_script = logs / "Audio_Mix_Filter.txt"
    filter_script.write_text(filter_graph + "\n", encoding="utf-8")

    # Stage the new mix at a sibling path so a failed mix doesn't blow away
    # the existing final.mp4. On success we promote it.
    mixed_tmp = run_dir / "final.audio_mixed.mp4"
    if mixed_tmp.exists():
        mixed_tmp.unlink()

    _run_ffmpeg(
        [
            "-i", str(source_for_mix),
            *extra_inputs,
            "-filter_complex_script", str(filter_script),
            "-map", "0:v", "-c:v", "copy",
            "-map", f"[{final_label}]",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-shortest",
            # Drop the music mp3's ID3 tag: ffmpeg otherwise writes it as a QT
            # chapter ("text"/SubtitleHandler) track whose single sample spans the
            # whole mp3 (e.g. 193s), survives -shortest, and inflates the reported
            # duration far past the real content. -1 = copy from no input.
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-movflags", "+faststart",
            str(mixed_tmp),
        ]
    )

    # Promote the new mix. Preserve the first-ever voice-only backup; only
    # create it from final.mp4 on the very first mix.
    if not video_only_backup.exists():
        shutil.move(str(final_mp4), str(video_only_backup))
    elif final_mp4.exists():
        final_mp4.unlink()
    shutil.move(str(mixed_tmp), str(final_mp4))

    result = {
        "status": "ok",
        "final_path": str(final_mp4),
        "video_only_backup": str(video_only_backup),
        "music_file": (music or {}).get("file"),
        "n_sfx_cues": len(cues),
        "unresolved_cues": unresolved_cues,
        "n_unresolved_cues": len(unresolved_cues),
        "voice_source_lufs": voice_lufs,
        "voice_gain_db": voice_gain_db,
        "master_gain_db": round(MASTER_TARGET_LUFS - VOICE_TARGET_LUFS, 2),
        "voice_cleanup": _voice_cleanup_enabled(),
        "sfx_gain_by_file": sfx_gain_by_file,
    }
    _write_report(run_dir, result)
    return result


def _write_report(run_dir: Path, payload: dict) -> None:
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "Audio_Mix_Report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", help="The video id (run dir name).")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[4]
    run_dir = repo_root / "runs" / str(args.video_id)

    try:
        result = mix(run_dir)
    except AudioMixError as exc:
        msg = str(exc)
        print(f"ERROR: {msg}", file=sys.stderr)
        _write_report(run_dir, {"status": "error", "error": msg})
        return 1
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: unexpected: {msg}", file=sys.stderr)
        _write_report(run_dir, {"status": "error", "error": msg})
        return 2

    # mix() already wrote the report; don't write it a second time here.
    print(f"OK: {result['status']} -> {result.get('final_path', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
