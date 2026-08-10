"""Expand Scenes_Plan.json (short_planner output) into Scenes_Request.json.

The short_planner agent returns 0-N mid-clip scene SLOTS with absolute
timing on `source.mp4` (timing only — the author decides each visual).
This helper joins each scene against `transcript.json` to slice per-scene
word timings, validates the time budget against the source duration + the
reserved source-end margin, and writes one authoritative payload per scene
with scene-relative word timings and the slot duration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contenido_bionico.shared.run_ids import run_number

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNS = REPO_ROOT / "runs"

# Word-free padding the validator adds at EACH end of the planner's content
# span to form the full-bleed slot the author animates: a lead-in (background
# fades in, first element waits) and a tail (the finished composition dwells
# before the exit envelope removes it). The author's word list is sliced to
# the content span only and offset by the lead, so its first word lands
# ~PAD_SECONDS into the slot and its last word ends >= PAD_SECONDS before the
# slot ends. The lead stays tight (~1 s); the tail is ADAPTIVE — it grows
# beyond PAD_SECONDS up to TAIL_MAX_SECONDS when the extra time is actually
# free (see step 3b in build()), so viewers can read the completed visual.
PAD_SECONDS = 1.0
# Adaptive dwell tail cap: the slot may extend up to this long after the
# content's last word, never past the source-end margin and never eating the
# camera-only floor before the next slot.
TAIL_MAX_SECONDS = 3.0
# The planner writes time_start/time_end after reading a transcript whose
# times are rounded to 2 decimals (planner_transcript.txt uses %.2f), so its
# echoed boundary can land just AFTER the true word start (e.g. plan 5.10 vs
# raw 5.099) — without tolerance the content's FIRST word is silently
# dropped (observed in runs 25 and 26). Half the 0.01 s rounding step plus
# float slack covers exactly that error.
BOUNDARY_EPSILON_SECONDS = 0.006
# Earliest a (padded) slot may begin: the start of the video. The slot's
# word-free lead-in is PAD_SECONDS, so a scene needs its content to start at
# time_start >= ~PAD_SECONDS for the full lead to fit; an earlier content
# start simply gets its lead clamped at 0.
MIN_SCENE_START_SECONDS = 0.0
TAIL_RESERVED_SECONDS = 1.5
# HARD minimum camera-only window between two full-bleed scenes. The validator
# enforces this by merging two adjacent scenes into one longer slot whenever
# possible, or dropping the second scene otherwise. Prevents the "face pops in
# for half a second between two animations" effect.
MIN_GAP_BETWEEN_SCENES = 5.0
# Camera-only floor kept in front of the NEXT slot when the adaptive dwell
# tail extends a scene. Derived from the existing constants: the planner
# guarantees >= 7 s between scenes' words, which after both 1 s pads leaves
# >= MIN_GAP_BETWEEN_SCENES (5 s) between slots; the dwell may consume at
# most one extra PAD_SECONDS of that, so at least 4 s of camera always
# remains between two full-bleed slots.
CAMERA_ONLY_FLOOR_SECONDS = MIN_GAP_BETWEEN_SCENES - PAD_SECONDS


class BuildScenesRequestError(RuntimeError):
    pass


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _word_text(row: dict[str, Any]) -> str | None:
    value = row.get("text")
    if isinstance(value, str):
        return value
    value = row.get("word")
    if isinstance(value, str):
        return value
    return None


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise BuildScenesRequestError(f"required file missing: {path}")
    if path.stat().st_size == 0:
        raise BuildScenesRequestError(f"required file is empty: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise BuildScenesRequestError(f"invalid JSON in {path}: {exc}") from exc


def _validate_plan_shape(plan: Any, video_id: str | int) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        raise BuildScenesRequestError(
            f"Scenes_Plan.json top-level must be an object, got {type(plan).__name__}"
        )
    try:
        plan_video_id = int(plan.get("video_id"))
    except (TypeError, ValueError):
        raise BuildScenesRequestError(
            f"Scenes_Plan.json video_id missing or not an integer: {plan.get('video_id')!r}"
        )
    expected_video_id = run_number(video_id)
    if plan_video_id != expected_video_id:
        raise BuildScenesRequestError(
            f"Scenes_Plan.json video_id mismatch: plan says {plan_video_id}, run dir is {video_id}"
        )
    scenes = plan.get("scenes")
    if scenes is None:
        return []
    if not isinstance(scenes, list):
        raise BuildScenesRequestError("Scenes_Plan.json scenes must be a list (may be empty)")
    return scenes


def _scene_words(
    content_start: float,
    content_end: float,
    slot_start: float,
    transcript_words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Slice the words spoken inside the CONTENT span [content_start, content_end)
    and express their timings relative to the (earlier) SLOT start. Because the
    slot starts PAD_SECONDS before the content, the first word lands ~PAD_SECONDS
    into the slot and the last ends ~PAD_SECONDS before it ends; the lead and
    tail padding regions stay word-free, giving the author room to settle the
    first element and dwell the last one before the exit envelope."""
    out: list[dict[str, Any]] = []
    for word in transcript_words:
        if not isinstance(word, dict):
            continue
        token_type = word.get("type") or "word"
        if token_type != "word":
            continue
        text = _word_text(word)
        if text is None or not text.strip():
            continue
        start = _coerce_number(word.get("start"))
        end = _coerce_number(word.get("end"))
        if start is None or end is None or end <= start:
            continue
        # Leading-edge tolerance: the planner echoes 2-decimal-rounded times,
        # so content_start may land a few ms AFTER the true start of the very
        # word it means to include — never drop that word (see
        # BOUNDARY_EPSILON_SECONDS).
        if not (content_start - BOUNDARY_EPSILON_SECONDS <= start < content_end):
            continue
        out.append(
            {
                "word": text.strip(),
                "start": round(max(0.0, start - slot_start), 3),
                "end": round(min(end, content_end) - slot_start, 3),
            }
        )
    return out


def build(
    plan_path: Path,
    transcript_path: Path,
    source_duration: float,
    out_path: Path,
    *,
    video_id: int,
) -> dict[str, Any]:
    plan = _load_json(plan_path)
    raw_scenes = _validate_plan_shape(plan, video_id)

    transcript = _load_json(transcript_path)
    transcript_words = transcript.get("words") if isinstance(transcript, dict) else None
    if not isinstance(transcript_words, list) or not transcript_words:
        raise BuildScenesRequestError(f"transcript.json has no words[]: {transcript_path}")

    if source_duration <= 0:
        raise BuildScenesRequestError(f"invalid source_duration: {source_duration}")

    slot_floor = MIN_SCENE_START_SECONDS
    max_end = max(0.0, source_duration - TAIL_RESERVED_SECONDS)

    # 1) Parse and validate each planner CONTENT span. time_start/time_end are
    #    the first/last spoken word of the content the planner wants visualized;
    #    the planner adds NO padding (this validator does, below).
    parsed: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for idx, scene in enumerate(raw_scenes):
        prefix = f"scenes[{idx}]"
        if not isinstance(scene, dict):
            raise BuildScenesRequestError(f"{prefix} is not an object")

        raw_segment_id = scene.get("segment_id")
        try:
            segment_id = int(raw_segment_id)
        except (TypeError, ValueError):
            raise BuildScenesRequestError(
                f"{prefix}.segment_id missing or not an integer: {raw_segment_id!r}"
            )
        if segment_id <= 0:
            raise BuildScenesRequestError(f"{prefix}.segment_id must be positive, got {segment_id}")
        if segment_id in seen_ids:
            raise BuildScenesRequestError(
                f"{prefix}.segment_id={segment_id} duplicates an earlier scene"
            )
        seen_ids.add(segment_id)

        content_start = _coerce_number(scene.get("time_start"))
        content_end = _coerce_number(scene.get("time_end"))
        if content_start is None or content_end is None:
            raise BuildScenesRequestError(
                f"{prefix} segment_id={segment_id}: time_start and time_end must be numbers, "
                f"got time_start={scene.get('time_start')!r} time_end={scene.get('time_end')!r}"
            )
        if not (content_start < content_end):
            raise BuildScenesRequestError(
                f"{prefix} segment_id={segment_id}: time_start ({content_start}) must be "
                f"< time_end ({content_end})"
            )
        parsed.append(
            {
                "segment_id": segment_id,
                "content_start": content_start,
                "content_end": content_end,
            }
        )

    # 2) Pad each content span into the full-bleed SLOT the author animates,
    #    clamped to the global [slot_floor, max_end] bounds. Keep the content
    #    span inside its slot so word slicing never reaches outside it.
    parsed.sort(key=lambda s: s["content_start"])
    for s in parsed:
        s["slot_start"] = max(slot_floor, s["content_start"] - PAD_SECONDS)
        s["slot_end"] = min(max_end, s["content_end"] + PAD_SECONDS)
        s["content_start"] = max(s["content_start"], s["slot_start"])
        s["content_end"] = min(s["content_end"], s["slot_end"])

    # 3) Enforce non-overlap and the minimum camera-only gap between SLOTS. Two
    #    slots closer than MIN_GAP_BETWEEN_SCENES are merged into one longer
    #    slot (extending the earlier slot and its content to swallow the later).
    #    There is no duration cap: the planner decides scene length from content.
    resolved: list[dict[str, Any]] = []
    for s in parsed:
        if not resolved:
            resolved.append(s)
            continue
        prev = resolved[-1]
        if s["slot_start"] - prev["slot_end"] >= MIN_GAP_BETWEEN_SCENES:
            resolved.append(s)
            continue
        prev["slot_end"] = max(prev["slot_end"], s["slot_end"])
        prev["content_end"] = max(prev["content_end"], s["content_end"])

    # 3b) Adaptive dwell tail: after the last word lands, viewers need time to
    #     read the completed composition, so extend each slot's tail beyond the
    #     base PAD_SECONDS up to TAIL_MAX_SECONDS after the content — but only
    #     into time that is actually free: never past max_end (the reserved
    #     source ending) and never closer than CAMERA_ONLY_FLOOR_SECONDS to the
    #     next slot's leading edge. Extension only — a slot never shrinks.
    for idx, s in enumerate(resolved):
        dwell_end = min(s["content_end"] + TAIL_MAX_SECONDS, max_end)
        if idx + 1 < len(resolved):
            dwell_end = min(
                dwell_end,
                resolved[idx + 1]["slot_start"] - CAMERA_ONLY_FLOOR_SECONDS,
            )
        s["slot_end"] = max(s["slot_end"], dwell_end)

    # 4) Slice each scene's words from its content span (offset to the slot
    #    start so the lead/tail padding stays word-free) and emit the request.
    scenes: list[dict[str, Any]] = []
    for s in resolved:
        segment_id = s["segment_id"]
        slot_start = s["slot_start"]
        slot_end = s["slot_end"]
        if not (slot_start < slot_end):
            raise BuildScenesRequestError(
                f"segment_id={segment_id}: slot collapsed after clamping "
                f"(start={slot_start:.2f}s, end={slot_end:.2f}s)"
            )
        duration = slot_end - slot_start

        words = _scene_words(
            s["content_start"], s["content_end"], slot_start, transcript_words
        )
        if not words:
            raise BuildScenesRequestError(
                f"segment_id={segment_id}: no transcript words start inside the content "
                f"span [{s['content_start']:.2f}, {s['content_end']:.2f})"
            )

        scenes.append(
            {
                "segment_id": segment_id,
                "time_start": round(slot_start, 3),
                "time_end": round(slot_end, 3),
                "timing": {
                    "duration": round(duration, 3),
                    "words": words,
                },
            }
        )

    scenes.sort(key=lambda row: row["segment_id"])

    payload: dict[str, Any] = {
        "video_id": str(run_number(video_id)),
        "source_duration": round(source_duration, 3),
        "scenes": scenes,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_from_plan(
    video_id: int,
    *,
    source_duration: float,
) -> dict[str, Any]:
    run_dir = RUNS / str(video_id)
    return build(
        plan_path=run_dir / "requests" / "Scenes_Plan.json",
        transcript_path=run_dir / "transcript.json",
        source_duration=source_duration,
        out_path=run_dir / "requests" / "Scenes_Request.json",
        video_id=video_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("--source-duration", required=True, type=float)
    args = parser.parse_args(argv)
    try:
        payload = build_from_plan(
            args.video_id,
            source_duration=args.source_duration,
        )
    except BuildScenesRequestError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        f"build_scenes_request: video_id={args.video_id} "
        f"scenes={len(payload['scenes'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
