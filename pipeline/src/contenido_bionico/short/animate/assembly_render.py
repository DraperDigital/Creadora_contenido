"""Assemble the final vertical short.

The short pipeline treats the cut source (the cut output `source.mp4`) as
the one and only short for this run. There is no clip extraction. Layers,
z-order bottom to top:

  1. source.mp4 (1080x1920 talking head, audio bed) routed through the shared
     deterministic camera (`camera.source_chain`): sentence-based top-half
     punch zooms + a constant smooth drift.
  2. captions.webm (Remotion-rendered, centered alpha overlay) — above the
     talking head but BELOW the animations.
  3. N visual scene WebMs (alpha) overlaid each at its absolute `time_start`.

Filter graph (N scenes):

    [0:v]scale...,pad...,setsar=1,fps=30,zoompan=...[base];
    [1:v]scale...,fps=30,setpts=PTS-STARTPTS+t1/TB[scene1];
    [2:v]scale...,fps=30,setpts=PTS-STARTPTS+t2/TB[scene2];
    [3:v]scale...,fps=30,setpts=PTS-STARTPTS[caps];
    [base][caps]overlay=0:0:format=auto:eof_action=pass:repeatlast=0[pre1];
    [pre1][scene1]overlay=0:0:...:enable='between(t,t1,t1e)'[pre2];
    [pre2][scene2]overlay=0:0:...:enable='between(t,t2,t2e)'[vout]

Captions are optional (None omits the captions input + overlay). Visual
scenes are optional (zero scenes = camera base + centered captions). Captions
sit just above the source so the full-bleed animations composite ON TOP of
them. With every optional layer off, the camera base alone is the whole video.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Resolve the package from THIS source tree first when run by path.
SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contenido_bionico.shared import camera  # noqa: E402
from contenido_bionico.shared.camera import CameraPlan  # noqa: E402
from contenido_bionico.shared.ffmpeg import NO_WINDOW, X264_BASELINE  # noqa: E402

OUT_W = 1080
OUT_H = 1920
OUT_FPS = 30


class AssemblyError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisualScene:
    """One mid-clip visual layer scheduled at an absolute `time_start` on the source."""
    segment_id: int
    time_start: float
    time_end: float
    webm_path: Path


def _run_ffmpeg(args: list[str], cwd: Path) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise AssemblyError(
            f"ffmpeg failed (exit {proc.returncode}). Cmd: {' '.join(cmd)}\n"
            f"cwd: {cwd}\n"
            f"stderr:\n{proc.stderr}"
        )


def _overlay_clause(prev_tag: str, layer_tag: str, out_tag: str, enable: str | None = None) -> str:
    enable_part = f":enable='{enable}'" if enable else ""
    return (
        f"[{prev_tag}][{layer_tag}]"
        f"overlay=0:0:format=auto:eof_action=pass:repeatlast=0{enable_part}"
        f"[{out_tag}]"
    )


def _build_filter_complex(
    *,
    camera_plan: CameraPlan,
    visual_scenes: Sequence[VisualScene],
    has_captions: bool,
    captions_input_idx: int | None,
) -> str:
    """Layer order, bottom -> top: camera base, scenes, captions.

    The base layer is built by the shared deterministic camera
    (`camera.source_chain`): the cut source is scaled/padded to the canonical
    1080x1920 30fps frame and routed through `zoompan` for the sentence-based
    top-half punches + constant drift. Scene WebMs are pts-shifted to
    their scheduled start times; overlays composite in order. The
    Remotion-rendered captions.webm (when present) composites LAST so a
    full-bleed scene that covers the entire frame does not hide the word-level
    captions. Captions are optional: when `has_captions` is False no captions
    clause is emitted and no captions input index is referenced.
    """
    parts: list[str] = []

    # Base layer: the cut source video routed through the shared camera.
    parts.append(
        camera.source_chain(camera_plan, out_w=OUT_W, out_h=OUT_H, fps=OUT_FPS)
    )

    input_idx = 1
    scene_layers: list[tuple[str, float, float]] = []
    for scene in visual_scenes:
        label = f"scene{scene.segment_id}"
        parts.append(
            f"[{input_idx}:v]"
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
            f"setsar=1,fps={OUT_FPS},"
            f"setpts=PTS-STARTPTS+{scene.time_start:.6f}/TB"
            f"[{label}]"
        )
        scene_layers.append((label, scene.time_start, scene.time_end))
        input_idx += 1

    # Captions.webm (centered Remotion alpha overlay) is composited ABOVE the
    # talking-head source but BELOW the full-bleed animations, so a scene
    # renders ON TOP of the centered captions (full-bleed scenes cover them
    # during their windows; captions show through when only the talking head
    # is on screen). Optional: emit the caps clause only when captions are
    # present.
    caps_label: str | None = None
    if has_captions:
        parts.append(
            f"[{captions_input_idx}:v]"
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
            f"setsar=1,fps={OUT_FPS},setpts=PTS-STARTPTS[caps]"
        )
        caps_label = "caps"

    # Overlay stack, bottom -> top: source (camera) -> captions -> scenes.
    overlay_layers: list[tuple[str, str | None]] = []
    if caps_label:
        overlay_layers.append((caps_label, None))
    for label, ts, te in scene_layers:
        enable = f"between(t,{ts:.6f},{te:.6f})" if te > ts else None
        overlay_layers.append((label, enable))

    if not overlay_layers:
        # No captions, no scenes: the camera base IS the whole video.
        # Relabel it to [vout] (a no-op copy) so the caller's -map "[vout]" is
        # always satisfiable, whatever combination of optional layers is off.
        parts.append("[base]copy[vout]")
        return ";".join(parts)

    prev = "base"
    for i, (layer, enable) in enumerate(overlay_layers):
        out = "vout" if i == len(overlay_layers) - 1 else f"pre{i + 1}"
        parts.append(_overlay_clause(prev, layer, out, enable=enable))
        prev = out

    return ";".join(parts)


def assemble_final(
    run_dir: Path,
    *,
    source_video: Path,
    captions_webm: Path | None,
    visual_scenes: Sequence[VisualScene] = (),
    camera_plan: CameraPlan,
    output_mp4: Path,
) -> dict[str, object]:
    if not run_dir.exists():
        raise AssemblyError(f"run dir does not exist: {run_dir}")
    if not source_video.exists():
        raise AssemblyError(f"source video missing: {source_video}")
    # Captions are optional: None omits the layer; a non-None path must exist
    # and be non-empty.
    if captions_webm is not None and (not captions_webm.exists() or captions_webm.stat().st_size == 0):
        raise AssemblyError(f"captions.webm missing or empty: {captions_webm}")

    sorted_scenes = sorted(visual_scenes, key=lambda s: s.time_start)
    for scene in sorted_scenes:
        if not scene.webm_path.exists() or scene.webm_path.stat().st_size == 0:
            raise AssemblyError(f"visual scene webm missing or empty: {scene.webm_path}")

    def _rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(run_dir.resolve()))
        except ValueError:
            return str(p.resolve())

    output_basename = output_mp4.name
    source_basename = _rel(source_video)

    inputs: list[str] = ["-i", source_basename]
    for scene in sorted_scenes:
        inputs += ["-c:v", "libvpx-vp9", "-i", _rel(scene.webm_path)]
    # captions.webm (when present) is decoded as the LAST input (TOP overlay).
    # Its index is only computed/appended when captions are present, so an
    # omitted captions layer leaves no dangling input reference in the graph.
    captions_input_idx: int | None = None
    if captions_webm is not None:
        captions_input_idx = 1 + len(sorted_scenes)
        inputs += ["-c:v", "libvpx-vp9", "-i", _rel(captions_webm)]

    filter_complex = _build_filter_complex(
        camera_plan=camera_plan,
        visual_scenes=sorted_scenes,
        has_captions=captions_webm is not None,
        captions_input_idx=captions_input_idx,
    )
    # The camera's zoompan expression grows with the punch-keyframe count, so
    # the graph goes through a filter_complex *script file* (same pattern as
    # shared/desilence.py) instead of an inline argv entry — an arbitrarily
    # long expression cannot overflow the OS command-line limit.
    filter_script = run_dir / "_assembly_filter_complex.txt"
    filter_script.write_text(filter_complex, encoding="utf-8")

    cmd_tail = [
        "-filter_complex_script", filter_script.name,
        "-map", "[vout]",
        "-map", "0:a",
        *X264_BASELINE,
        "-c:a", "aac",
        # 320k: this output is an INTERMEDIATE (the audio mix re-encodes it),
        # so the extra bitrate is cheap insurance against generation loss.
        "-b:a", "320k",
        "-r", str(OUT_FPS),
        "-movflags", "+faststart",
        "-shortest",
        output_basename,
    ]

    _run_ffmpeg([*inputs, *cmd_tail], cwd=run_dir)

    out = run_dir / output_basename
    if not out.exists() or out.stat().st_size == 0:
        raise AssemblyError(f"ffmpeg succeeded but output is missing or empty: {out}")
    return {
        "output_path": str(out),
        "captions_path": str(captions_webm) if captions_webm else None,
        "visual_scenes": [
            {
                "segment_id": s.segment_id,
                "time_start": s.time_start,
                "time_end": s.time_end,
                "webm_path": str(s.webm_path),
            }
            for s in sorted_scenes
        ],
    }


def write_assembly_manifest(
    run_dir: Path,
    *,
    source_video: Path,
    captions_webm: Path | None,
    visual_scenes: Sequence[VisualScene],
    camera_plan: CameraPlan,
    output_mp4: Path,
    skipped_scenes: Sequence[dict[str, object]] = (),
) -> Path:
    def _maybe_rel(p: Path | None) -> str | None:
        if p is None:
            return None
        try:
            return str(p.resolve().relative_to(run_dir.resolve()))
        except ValueError:
            return str(p.resolve())

    manifest = {
        "source_video": _maybe_rel(source_video),
        "captions_webm": _maybe_rel(captions_webm),
        "camera": camera_plan.to_dict(),
        "visual_scenes": [
            {
                "segment_id": s.segment_id,
                "time_start": s.time_start,
                "time_end": s.time_end,
                "webm_path": _maybe_rel(s.webm_path),
            }
            for s in sorted(visual_scenes, key=lambda x: x.time_start)
        ],
        "skipped_scenes": list(skipped_scenes),
        "output_mp4": _maybe_rel(output_mp4),
    }
    path = run_dir / "Short_Assembly.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def assemble_from_manifest(run_dir: Path) -> dict[str, object]:
    manifest_path = run_dir / "Short_Assembly.json"
    if not manifest_path.exists():
        raise AssemblyError(f"Short_Assembly.json missing under {run_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    source = run_dir / manifest["source_video"]
    captions_raw = manifest.get("captions_webm")
    captions = (run_dir / captions_raw) if captions_raw else None
    output = run_dir / manifest["output_mp4"]
    camera_plan = CameraPlan.from_dict(manifest["camera"])
    scenes: list[VisualScene] = []
    for entry in manifest.get("visual_scenes") or []:
        scenes.append(
            VisualScene(
                segment_id=int(entry["segment_id"]),
                time_start=float(entry["time_start"]),
                time_end=float(entry["time_end"]),
                webm_path=run_dir / entry["webm_path"],
            )
        )
    return assemble_final(
        run_dir,
        source_video=source,
        captions_webm=captions,
        visual_scenes=scenes,
        camera_plan=camera_plan,
        output_mp4=output,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = assemble_from_manifest(args.run_dir)
    except AssemblyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {result['output_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
