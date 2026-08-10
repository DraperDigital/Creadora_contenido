"""Static-image producers (category `imagenes`), all 1080x1350 PNGs.

Each `produce_<x>(ctx) -> list[Path]` writes into `ctx.out_dir` and returns the
published paths; it returns `[]` to skip (missing atoms / no b-roll) and raises
only on a genuine render failure (the runner logs and continues).

Text is LLM-fitted to each format (V6.1): the quote/phrase producers consume
`frase_adapt` (`adapted_citas` / `adapted_frase` / `hero_word`, one cached opus
call per run that degrades to the raw quote plan), and `produce_infografia`
runs its own structured agent (`agents/infografia.md`) whose length caps ARE
the layout contract; on any agent failure it falls back to the mechanical
carousel extraction bounded by the same caps.

Three families:
  - comp renders: FraseImagen / Infografia / CitasTresEnUno stills
    (`cita_N.png` are now rendered quote cards, no longer copies).
  - b-roll: `cita_foto_*`, `frase_foto` stage a user photo into the Remotion
    public assets under `assets/runs/<video_id>/broll/` and pass a relative
    staticFile src. That path mirrors the scenes' asset namespace, so
    `_prune_remotion_run_artifacts` cleans it after the run.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from contenido_bionico.shared.remotion_manifest_formats import render_format_stills
from contenido_bionico.shared.remotion_runtime import REMOTION_PUBLIC
from contenido_bionico.short.formats import adapt, broll, frase_adapt

WIDTH = 1080
HEIGHT = 1350

_FRASE_COMP = "formats/FraseImagen"
_FRASE_ID = "frase-imagen"
_INFOGRAFIA_COMP = "formats/Infografia"
_INFOGRAFIA_ID = "infografia"
_CITAS_COMP = "formats/CitasTresEnUno"
_CITAS_ID = "citas-3en1"

# Infografia structural caps (mirrored in agents/infografia.md — the layout is
# sized so max-cap text at 5 points still fits 1080x1350 with margins).
INFOGRAFIA_PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "infografia.md"
INFOGRAFIA_TITLE_MAX = 48
INFOGRAFIA_EYEBROW_MAX = 24
INFOGRAFIA_HEADING_MAX = 36
INFOGRAFIA_BODY_MAX = 90
INFOGRAFIA_MIN_POINTS = 3   # fewer usable agent points than this => fallback
INFOGRAFIA_MAX_POINTS = 5


# --- helpers ------------------------------------------------------------------


def _render_still(*, composition_id: str, component_file: str, props: dict, out_png: Path) -> Path:
    """Render one FraseImagen/Infografia/CitasTresEnUno frame to a 1080x1350 PNG."""
    render_format_stills(
        composition_id=composition_id,
        component_file=component_file,
        width=WIDTH,
        height=HEIGHT,
        duration_frames=1,
        props=props,
        out_pngs=[out_png],
        frames=[0],
    )
    return out_png


def _clear(out_dir: Path, pattern: str) -> None:
    """Delete stale outputs whose name matches `pattern` exactly (own files only)."""
    rx = re.compile(pattern)
    for p in out_dir.iterdir():
        if p.is_file() and rx.match(p.name):
            try:
                p.unlink()
            except OSError:
                pass


def _broll_stage_dir(video_id: str) -> Path:
    return REMOTION_PUBLIC / "assets" / "runs" / str(video_id) / "broll"


def _safe_asset_name(photo: Path, *, cutout: bool = False) -> str:
    """A URL/render-safe, collision-free staged filename for `photo`.

    Slugs the stem (ascii word chars only) and appends a short path hash so two
    different photos never share a staged name.
    """
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", photo.stem)[:40].strip("_") or "foto"
    digest = hashlib.sha1(str(photo.resolve()).encode("utf-8")).hexdigest()[:8]
    if cutout:
        return f"{stem}_{digest}_cutout.png"
    return f"{stem}_{digest}{photo.suffix.lower()}"


def _stage_photo(video_id: str, photo: Path) -> str:
    """Copy `photo` into the run's broll staging dir; return its staticFile src."""
    dest_dir = _broll_stage_dir(video_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_asset_name(photo)
    dest = dest_dir / name
    data = photo.read_bytes()
    if not dest.exists() or dest.stat().st_size != len(data):
        dest.write_bytes(data)
    return f"assets/runs/{video_id}/broll/{name}"


def _truncate_chars(text: str, limit: int) -> str:
    """Cut `text` to <= `limit` chars at a word boundary (no trailing ellipsis)."""
    text = adapt.clean_line(text)
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut[:limit].strip()


# --- producers ----------------------------------------------------------------


def produce_cita_estatica(ctx):
    """Each adapted cita as a rendered branded quote card, `cita_N.png`.

    No longer copies `runs/<id>/quotes/quote_N.png` — every card is a fresh
    FraseImagen "plantilla" still over the LLM-fitted quote text.
    """
    texts = frase_adapt.adapted_citas(ctx)
    _clear(ctx.out_dir, r"^cita_\d+\.png$")
    if not texts:
        return []
    palette = ctx.palette()
    out: list[Path] = []
    for i, text in enumerate(texts, start=1):
        dest = ctx.out_dir / f"cita_{i}.png"
        _render_still(
            composition_id=_FRASE_ID,
            component_file=_FRASE_COMP,
            props={
                "style": "plantilla",
                "text": text,
                "attribution": "",
                "eyebrow": "CITA",
                "palette": palette,
            },
            out_png=dest,
        )
        out.append(dest)
    return out


def produce_cita_foto(ctx):
    """Top-3 adapted citas over the first-3 photos (paired) as `cita_foto_N.png`."""
    texts = frase_adapt.adapted_citas(ctx)[:3]
    photos = broll.select_photos(ctx, 3, "cita")
    pairs = list(zip(texts, photos))
    if not pairs:
        return []
    palette = ctx.palette()
    _clear(ctx.out_dir, r"^cita_foto_\d+\.png$")
    out: list[Path] = []
    for i, (text, photo) in enumerate(pairs, start=1):
        photo_src = _stage_photo(ctx.video_id, photo)
        dest = ctx.out_dir / f"cita_foto_{i}.png"
        _render_still(
            composition_id=_FRASE_ID,
            component_file=_FRASE_COMP,
            props={
                "style": "foto",
                "text": text,
                "attribution": "",
                "photoSrc": photo_src,
                "palette": palette,
            },
            out_png=dest,
        )
        out.append(dest)
    return out


def produce_citas_3en1(ctx):
    """Top 3 adapted citas (>=2) stacked in one branded `citas_3en1.png`."""
    texts = frase_adapt.adapted_citas(ctx)[:3]
    if len(texts) < 2:
        return []
    dest = ctx.out_dir / "citas_3en1.png"
    _render_still(
        composition_id=_CITAS_ID,
        component_file=_CITAS_COMP,
        props={"quotes": texts, "palette": ctx.palette()},
        out_png=dest,
    )
    return [dest]


# --- infografia ----------------------------------------------------------------


def _infografia_classic(ctx) -> dict | None:
    """Mechanical extraction from the carousel plan (uncapped), or None."""
    plan = ctx.carousel_plan() or {}
    slides = plan.get("slides") or []
    hero = next((s for s in slides if isinstance(s, dict) and s.get("kind") == "hero"), None)
    title = str((hero or {}).get("title") or plan.get("title") or "").strip()
    eyebrow = str((hero or {}).get("eyebrow") or "").strip()
    points: list[dict] = []
    for s in slides:
        if isinstance(s, dict) and s.get("kind") == "item" and str(s.get("heading") or "").strip():
            points.append(
                {"heading": str(s["heading"]).strip(), "body": str(s.get("body") or "").strip()}
            )
    if not title or not points:
        return None
    return {"title": title, "eyebrow": eyebrow, "points": points}


def _cap_infografia(title: str, eyebrow: str, points: list[dict]) -> dict:
    """Enforce the structural caps: truncate every string, slice to max points.

    Both the agent payload and the mechanical fallback pass through here, so
    the layout constraint holds regardless of where the content came from.
    """
    capped: list[dict] = []
    for p in points:
        if not isinstance(p, dict):
            continue
        heading = _truncate_chars(p.get("heading"), INFOGRAFIA_HEADING_MAX)
        if not heading:
            continue
        capped.append(
            {"heading": heading, "body": _truncate_chars(p.get("body"), INFOGRAFIA_BODY_MAX)}
        )
    return {
        "title": _truncate_chars(title, INFOGRAFIA_TITLE_MAX),
        "eyebrow": _truncate_chars(eyebrow, INFOGRAFIA_EYEBROW_MAX),
        "points": capped[:INFOGRAFIA_MAX_POINTS],
    }


def _puntos_reference(classic: dict) -> str:
    """The mechanical extraction as the grounding `<PUNTOS>` block."""
    lines = [f"Titulo: {classic['title']}"]
    for i, p in enumerate(classic["points"], start=1):
        line = f"{i}. {p['heading']}"
        if p.get("body"):
            line += f" - {p['body']}"
        lines.append(line)
    return "\n".join(lines)


def _infografia_adapted(ctx, classic: dict) -> dict:
    """Run the infografia agent and validate/cap its payload.

    Raises on ANY failure (no transcript, agent unavailable, bad payload) —
    `produce_infografia` falls back to the bounded mechanical extraction.
    """
    text = adapt.transcript_text(ctx.transcript())
    if not text:
        raise adapt.AdaptError("transcript has no spoken text")
    message = "\n".join(
        [
            "Run the Infografia job.",
            "Return only the JSON object on stdout. Do not use tools or read files.",
            adapt.tagged("TRANSCRIPT", text),
            adapt.tagged("PUNTOS", _puntos_reference(classic)),
        ]
    )
    data = adapt.call_json_agent(
        ctx,
        prompt_path=INFOGRAFIA_PROMPT_PATH,
        log_name="infografia_adapt",
        message=message,
    )
    points = data.get("points")
    if not isinstance(points, list):
        raise adapt.AdaptError("infografia payload has no points list")
    capped = _cap_infografia(
        str(data.get("title") or ""), str(data.get("eyebrow") or ""), points
    )
    if not capped["title"]:
        raise adapt.AdaptError("infografia payload has an empty title")
    if len(capped["points"]) < INFOGRAFIA_MIN_POINTS:
        raise adapt.AdaptError(
            f"infografia payload has {len(capped['points'])} usable points "
            f"(< {INFOGRAFIA_MIN_POINTS})"
        )
    if not capped["eyebrow"]:
        capped["eyebrow"] = _truncate_chars(classic["eyebrow"], INFOGRAFIA_EYEBROW_MAX)
    return capped


def produce_infografia(ctx):
    """Agent-structured key points as three infographic layouts: `infografia_1..3.png`.

    Content comes from the infografia agent (title/eyebrow/3-5 points, written
    inside the structural caps); any failure falls back to the mechanical
    carousel extraction bounded by the SAME caps, so no content can ever
    overflow the composition.
    """
    classic = _infografia_classic(ctx)
    if classic is None:
        return []
    try:
        data = _infografia_adapted(ctx, classic)
    except Exception as exc:  # noqa: BLE001 — spec: any adaptation failure falls back
        print(
            f"[formats] infografia: adaptador no disponible, uso extraccion mecanica ({exc})",
            file=sys.stderr,
            flush=True,
        )
        data = _cap_infografia(classic["title"], classic["eyebrow"], classic["points"])
    if not data["title"] or not data["points"]:
        return []
    palette = ctx.palette()
    out: list[Path] = []
    for variant in (1, 2, 3):
        dest = ctx.out_dir / f"infografia_{variant}.png"
        _render_still(
            composition_id=_INFOGRAFIA_ID,
            component_file=_INFOGRAFIA_COMP,
            props={
                "variant": variant,
                "title": data["title"],
                "eyebrow": data["eyebrow"],
                "points": data["points"],
                "palette": palette,
            },
            out_png=dest,
        )
        out.append(dest)
    return out


# --- frase_* --------------------------------------------------------------------


def _produce_frase(ctx, style: str, filename: str):
    """Render the adapted poster phrase through one FraseImagen text style."""
    text = frase_adapt.adapted_frase(ctx)
    if not text:
        return []
    dest = ctx.out_dir / filename
    _render_still(
        composition_id=_FRASE_ID,
        component_file=_FRASE_COMP,
        props={
            "style": style,
            "text": text,
            "attribution": "",
            "palette": ctx.palette(),
        },
        out_png=dest,
    )
    return [dest]


def produce_frase_solida(ctx):
    """Adapted poster phrase as giant hero text on a solid accent field."""
    return _produce_frase(ctx, "solida", "frase_solida.png")


def produce_frase_plantilla(ctx):
    """Adapted poster phrase on a branded paper template (Style_Tokens driven)."""
    return _produce_frase(ctx, "plantilla", "frase_plantilla.png")


def produce_frase_foto(ctx):
    """Adapted poster phrase as hero text over a user photo."""
    text = frase_adapt.adapted_frase(ctx)
    photos = broll.select_photos(ctx, 1, "frase")
    if not text or not photos:
        return []
    photo_src = _stage_photo(ctx.video_id, photos[0])
    dest = ctx.out_dir / "frase_foto.png"
    _render_still(
        composition_id=_FRASE_ID,
        component_file=_FRASE_COMP,
        props={
            "style": "foto",
            "text": text,
            "attribution": "",
            "photoSrc": photo_src,
            "palette": ctx.palette(),
        },
        out_png=dest,
    )
    return [dest]


# frase_detras was removed: the poster formats (formats/posters.py) are the
# corrected version of that "giant type behind the subject" creative.
