"""Carousel orchestrator: finished short run -> 4:5 PNG carousel slides.

Adapted from the ranking tier-list carousel. A framework/explainer short has no
tier list, so the carousel uses a "key points" model built from the transcript
ALONE (no ranking spec):

  1. Read the run's corrected transcript.
  2. Ask the `carousel_author` agent for {title, eyebrow, points:[{heading, body}]}.
  3. Build neutral slides (hero + one per key point, numbered chips).
  4. Render each slide to a PNG still (1080x1350) via Remotion.

Output: `runs/<id>/carousel/slide_01.png ...`. Best-effort and re-runnable; the
caller (pipeline) publishes the PNGs to the output folder.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contenido_bionico.shared import config  # noqa: E402
from contenido_bionico.shared.remotion_manifest_carousel import (  # noqa: E402
    write_carousel_compositions_ts,
)
from contenido_bionico.shared.render_remotion import (  # noqa: E402
    RemotionRenderError,
    render_isolated_stills,
)
from contenido_bionico.shared.runtime.agent_runner import (  # noqa: E402
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)

RUNS_DIR = REPO_ROOT / "runs"
PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "carousel_author.md"

AGENT_MODEL = "claude-opus-4-8"
AGENT_EFFORT = "medium"
AGENT_TIMEOUT_S = 300
AGENT_MAX_TURNS = 12

# Hero + up to MAX_CONTENT_SLIDES key points (so at most 8 slides total). The
# author returns as many strong points as the talk genuinely has; we never pad,
# and we trim anything past the cap so a carousel never bloats.
MAX_CONTENT_SLIDES = 7
DEFAULT_EYEBROW = "Guia rapida"


class CarouselError(RuntimeError):
    pass


def _tagged(name: str, value: str) -> str:
    return f"<{name}>\n{value}\n</{name}>"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _transcript_text(transcript: dict[str, Any]) -> str:
    """The corrected spoken script: every word in order."""
    words = transcript.get("words") or []
    parts = [
        str(w.get("text") or w.get("word") or "").strip()
        for w in words
        if w.get("type") == "word"
    ]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _call_agent(run_dir: Path, message: str) -> str:
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise CarouselError(missing_agent_cmd_message())
    if not PROMPT_PATH.exists():
        raise CarouselError(f"carousel system prompt missing: {PROMPT_PATH}")
    log_dir = run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "carousel_author.log"
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=PROMPT_PATH.read_text(encoding="utf-8"),
        initial_message=message,
        cwd=REPO_ROOT,
        timeout_seconds=AGENT_TIMEOUT_S,
        max_turns=AGENT_MAX_TURNS,
        tools=[],
        model=AGENT_MODEL,
        effort=AGENT_EFFORT,
        permission_mode=None,
        log_path=log_path,
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-400:]
        raise CarouselError(f"carousel_author agent failed (exit {result.returncode}); {tail}")
    return result.stdout


def _parse_plan(stdout: str) -> dict[str, Any]:
    """Extract the JSON object the agent returned (tolerant of stray prose)."""
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise CarouselError(f"carousel_author returned no JSON object: {stdout[:200]!r}")
    try:
        data = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CarouselError(f"carousel_author JSON invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise CarouselError("carousel_author JSON is not an object")
    return data


def _build_slides(plan: dict[str, Any]) -> list[dict[str, Any]]:
    title = re.sub(r"\s+", " ", str(plan.get("title") or "").strip())
    if not title:
        raise CarouselError("carousel_author returned no title")
    eyebrow = re.sub(r"\s+", " ", str(plan.get("eyebrow") or "").strip()) or DEFAULT_EYEBROW

    points = [
        p for p in (plan.get("points") or [])
        if isinstance(p, dict) and str(p.get("heading") or "").strip()
    ][:MAX_CONTENT_SLIDES]
    if not points:
        raise CarouselError("carousel_author returned no usable points")

    total = len(points)
    slides: list[dict[str, Any]] = [
        {
            "kind": "hero",
            "eyebrow": eyebrow,
            "title": title,
            "footnote": f"{total} claves" if total != 1 else "1 clave",
        }
    ]
    for i, point in enumerate(points, start=1):
        heading = re.sub(r"\s+", " ", str(point.get("heading") or "").strip())
        body = re.sub(r"\s+", " ", str(point.get("body") or "").strip())
        slides.append(
            {
                "kind": "item",
                "index": i,
                "total": total,
                "heading": heading,
                "body": body,
            }
        )
    return slides


def generate_carousel(video_id: str | int, notes: str | None = None) -> list[Path]:
    """Build and render the carousel for a finished short run.

    Returns the list of rendered PNG paths under `runs/<id>/carousel/`.
    """
    run_dir = RUNS_DIR / str(video_id)
    transcript_path = run_dir / "transcript.json"
    if not transcript_path.exists():
        raise CarouselError(f"transcript.json not found under {run_dir}")

    config.load_env_into_process()
    transcript = _load_json(transcript_path)
    text = _transcript_text(transcript)
    if not text:
        raise CarouselError("transcript has no spoken text")

    parts = [
        "Run the Carousel Author job.",
        "Return only the JSON object on stdout. Do not use tools or read files.",
        _tagged("TRANSCRIPT", text),
    ]
    if notes:
        parts.append(_tagged("USER_CHANGE_REQUEST", notes))
        parts.append(
            "A previous carousel was already produced from this transcript. The user "
            "requested the changes above. Apply them; otherwise keep your normal behavior."
        )
    message = "\n".join(parts)

    print(f"[carousel] {video_id}: extracting key points via carousel_author", flush=True)
    plan = _parse_plan(_call_agent(run_dir, message))
    slides = _build_slides(plan)

    out_dir = run_dir / "carousel"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Drop any stale slides from a previous run so a shorter carousel never
    # leaves orphaned high-numbered PNGs behind.
    for stale in out_dir.glob("slide_*.png"):
        stale.unlink()
    out_pngs = [out_dir / f"slide_{i:02d}.png" for i in range(1, len(slides) + 1)]

    print(f"[carousel] {video_id}: rendering {len(out_pngs)} slides (1080x1350)", flush=True)
    try:
        render_isolated_stills(
            write_compositions=lambda p: write_carousel_compositions_ts(slides=slides, out_path=p),
            composition_id="carousel",
            out_pngs=out_pngs,
        )
    except RemotionRenderError as exc:
        raise CarouselError(f"carousel render failed: {exc}") from exc

    (out_dir / "Carousel_Plan.json").write_text(
        json.dumps({"title": slides[0]["title"], "slides": slides}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[carousel] {video_id}: done -> {out_dir}", flush=True)
    return out_pngs


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render a carousel from a short run.")
    parser.add_argument("video_id", help="run id, e.g. 1_short")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args(argv)
    try:
        pngs = generate_carousel(args.video_id, notes=args.notes)
    except CarouselError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {len(pngs)} slides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
