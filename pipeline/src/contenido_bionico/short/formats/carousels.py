"""Carousel-variant producers (category `carruseles`).

Every producer follows the contract: `produce_<x>(ctx) -> list[Path]` — render
the deliverable(s) into `ctx.out_dir` and return the published paths; return
`[]` to skip (missing atoms / no photos / agent unavailable); raise on a genuine
render failure (the runner catches, logs, and continues).

Design: all six layout variants share one prop shape
`{title, eyebrow, slides, palette}` and one frame contract — frame 0 is the hero
(synthesized in the comp from title/eyebrow), frames 1..N render `slides[N-1]`.
So `n_slides = len(slides) + 1`. The two RATIO variants (stories / cuadrado) do
NOT use a variant comp: they re-render the CLASSIC content through the shipping
`Carousel.tsx` at a different canvas, so they pass the raw plan slides (hero
included) straight through. The PDF variant assembles the already-published
classic PNGs with Pillow.

V6.1 adaptation: checklist / numeros / editorial / fotos each run a small
per-format agent (`agents/carrusel_<fmt>.md`, via `adapt.call_json_agent`) that
reads the final transcript and rewrites the content in the format's own voice
(real checklist items, listicle points, magazine lines, over-photo captions).
On ANY agent failure they fall back to the classic plan verbatim — a carousel
never disappears because an agent hiccuped. `citas` uses the shared
`frase_adapt.adapted_citas` fitting; stories / cuadrado / linkedin_pdf stay
ratio mirrors of the classic carousel (already agent-authored upstream).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from contenido_bionico.shared.remotion_manifest_formats import (  # noqa: E402
    render_format_stills,
)
from contenido_bionico.shared.remotion_runtime import REMOTION_PUBLIC  # noqa: E402
from contenido_bionico.shared.runtime.agent_runner import (  # noqa: E402
    missing_agent_cmd_message,
    resolve_agent_cmd,
    run_agent_code,
)
from contenido_bionico.short.formats import adapt, broll, frase_adapt  # noqa: E402

# Remotion composition id for every variant render. Each render is isolated (its
# own generated Compositions.<token>.ts holding a single composition), so one id
# is reused safely. Remotion ids allow [a-zA-Z0-9-] only — hence the hyphen.
_COMP_ID = "carousel-variant"

DEFAULT_EYEBROW = "Guia rapida"

CAROUSEL_WIDTH = 1080
CAROUSEL_HEIGHT = 1350
STORIES_SIZE = (1080, 1920)
SQUARE_SIZE = (1080, 1080)

# Per-format offsets so several carousels in one run land on DIFFERENT randomized
# canvases from broll_library/backgrounds/ (see broll.select_background(offset=)).
_BG_OFFSETS = {
    "numeros": 0, "checklist": 1, "editorial": 2, "citas": 3,
    "mitos": 4, "stories": 5, "cuadrado": 6,
}

# Final "follow me for more" CTA slide copy per static carousel (Spanish,
# on-brand, varied). checklist keeps its own "Guarda este post" closer (inline).
_CTA_TEXT = {
    "numeros": ("Sígueme para más", "Cada semana subo ideas como estas."),
    "editorial": ("Sígueme para más", "Más ediciones como esta cada semana."),
    "citas": ("Guarda y comparte", "Sígueme para más frases así."),
    "mitos": ("Sígueme para no perderte el resto", "Rompemos otro mito cada semana."),
    "stories": ("Sígueme para más", "Guarda y comparte si te sirvió."),
    "cuadrado": ("Sígueme para más", "Guarda este post para después."),
}

MITOS_PROMPT_PATH = Path(__file__).resolve().parent / "agents" / "mitos.md"
MITOS_MODEL = "claude-opus-4-8"
MITOS_EFFORT = "medium"
MITOS_TIMEOUT_S = 300
MITOS_MAX_TURNS = 8


# --- small helpers ------------------------------------------------------------

def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _tagged(name: str, value: str) -> str:
    return f"<{name}>\n{value}\n</{name}>"


def _slide_number(path: Path) -> int:
    match = re.search(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 0


def _published_classic_pngs(out_dir: Path) -> list[Path]:
    """The shipping classic-carousel PNGs (`carrusel_slide1.png`, …) sorted by
    their trailing slide number (numeric, so slide10 follows slide9)."""
    return sorted(out_dir.glob("carrusel_slide*.png"), key=_slide_number)


def _cta_slide(fmt: str) -> dict:
    """The final follow/save CTA slide (`kind: "cta"`) for static carousel `fmt`."""
    heading, body = _CTA_TEXT[fmt]
    return {"kind": "cta", "heading": heading, "body": body, "index": 0, "total": 0}


# --- Task 4.1: slide-data adapter + ratio scaffolding -------------------------

def classic_slides(ctx) -> dict | None:
    """`{"title","eyebrow","slides":[{kind,heading,body,index,total}...]}` from the
    published carousel plan, or None when there is no usable plan.

    Mirrors `carousel/orchestrator._build_slides`: the hero's title/eyebrow move
    to the top level and each key point becomes a re-indexed `item` slide. The
    hero is NOT emitted as a slide — the variant comps synthesize it from
    title/eyebrow at frame 0.
    """
    plan = ctx.carousel_plan()
    if not isinstance(plan, dict):
        return None
    raw = plan.get("slides") or []
    if not isinstance(raw, list):
        return None
    hero = next((s for s in raw if isinstance(s, dict) and s.get("kind") == "hero"), None)
    items = [
        s for s in raw
        if isinstance(s, dict) and s.get("kind") == "item" and _clean(s.get("heading"))
    ]
    title = _clean((hero or {}).get("title")) or _clean(plan.get("title"))
    eyebrow = _clean((hero or {}).get("eyebrow")) or DEFAULT_EYEBROW
    if not title or not items:
        return None
    total = len(items)
    slides = [
        {
            "kind": "item",
            "heading": _clean(it.get("heading")),
            "body": _clean(it.get("body")),
            "index": i,
            "total": total,
        }
        for i, it in enumerate(items, start=1)
    ]
    return {"title": title, "eyebrow": eyebrow, "slides": slides}


def _raw_classic_slides(ctx) -> list[dict] | None:
    """The published plan's slide list verbatim (hero + items), as the classic
    `Carousel.tsx` expects it. None when absent/empty."""
    plan = ctx.carousel_plan()
    if not isinstance(plan, dict):
        return None
    slides = plan.get("slides")
    if not isinstance(slides, list) or not slides:
        return None
    return slides


# --- V6.1: per-format content adaptation (agents/carrusel_<fmt>.md) -----------

# Validation bounds shared by every adapted carousel.
ADAPT_MIN_SLIDES = 3     # fewer usable slides than this => fall back to classic
ADAPT_MAX_SLIDES = 7     # agents are asked for 4-7; extra slides are trimmed
ADAPT_TITLE_MAX = 90     # hero titles clamp at ~5 lines; keep them sane
ADAPT_EYEBROW_MAX = 28   # kickers render on one letter-spaced uppercase line

_AGENTS_DIR = Path(__file__).resolve().parent / "agents"

# Per-format prompt + text caps. `heading`/`body` caps are characters unless the
# spec uses `*_words` (fotos: text sits over photos, so words are the unit).
ADAPT_SPECS: dict[str, dict[str, Any]] = {
    "checklist": {"prompt": "carrusel_checklist.md", "job": "Checklist",
                  "heading_chars": 40, "body_chars": 110},
    "numeros": {"prompt": "carrusel_numeros.md", "job": "Numeros",
                "heading_chars": 36, "body_chars": 110},
    "editorial": {"prompt": "carrusel_editorial.md", "job": "Editorial",
                  "heading_chars": 48, "body_chars": 120},
    "fotos": {"prompt": "carrusel_fotos.md", "job": "Fotos",
              "heading_words": 6, "body_words": 12},
}


def _truncate_chars(text: str, limit: int) -> str:
    """Cut `text` to <= `limit` chars at a word boundary (no trailing ellipsis)."""
    text = adapt.clean_line(text)
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut[:limit].strip()


def _truncate_word_count(text: str, limit: int) -> str:
    """Keep at most the first `limit` whitespace-separated words."""
    return " ".join(adapt.clean_line(text).split()[:limit])


def _puntos_reference(classic: dict) -> str:
    """The classic plan as the grounding `<PUNTOS>` block: title + numbered points."""
    lines = [f"Titulo: {classic['title']}"]
    for i, s in enumerate(classic["slides"], start=1):
        line = f"{i}. {s['heading']}"
        if _clean(s.get("body")):
            line += f" - {s['body']}"
        lines.append(line)
    return "\n".join(lines)


def _validate_adapted(data: dict, spec: dict, classic: dict) -> dict:
    """Clamp an agent payload into the `classic_slides` shape. Raises AdaptError
    when the payload cannot yield a publishable carousel."""
    title = _truncate_chars(adapt.clean_line(data.get("title")), ADAPT_TITLE_MAX)
    if not title:
        raise adapt.AdaptError("adapted payload has an empty title")
    eyebrow = (_truncate_chars(adapt.clean_line(data.get("eyebrow")), ADAPT_EYEBROW_MAX)
               or classic["eyebrow"])
    raw_slides = data.get("slides")
    if not isinstance(raw_slides, list):
        raise adapt.AdaptError("adapted payload has no slide list")
    rows: list[dict] = []
    for s in raw_slides:
        if not isinstance(s, dict):
            continue
        heading = adapt.clean_line(s.get("heading"))
        body = adapt.clean_line(s.get("body"))
        if "heading_words" in spec:
            heading = _truncate_word_count(heading, spec["heading_words"])
            body = _truncate_word_count(body, spec["body_words"])
        else:
            heading = _truncate_chars(heading, spec["heading_chars"])
            body = _truncate_chars(body, spec["body_chars"])
        if not heading:
            continue  # drop empty slides
        rows.append({"heading": heading, "body": body})
    rows = rows[:ADAPT_MAX_SLIDES]
    if len(rows) < ADAPT_MIN_SLIDES:
        raise adapt.AdaptError(
            f"adapted payload has {len(rows)} usable slides (< {ADAPT_MIN_SLIDES})"
        )
    total = len(rows)
    slides = [
        {"kind": "item", "heading": r["heading"], "body": r["body"],
         "index": i, "total": total}
        for i, r in enumerate(rows, start=1)
    ]
    return {"title": title, "eyebrow": eyebrow, "slides": slides}


def _adapted_slides(ctx, fmt: str, classic: dict) -> dict:
    """Run the per-format adaptation agent over the final transcript and return
    validated `{"title","eyebrow","slides"}` content. Raises on ANY failure
    (no transcript, agent unavailable, bad payload) — the caller falls back."""
    spec = ADAPT_SPECS[fmt]
    text = adapt.transcript_text(ctx.transcript())
    if not text:
        raise adapt.AdaptError("transcript has no spoken text")
    message = "\n".join([
        f"Run the {spec['job']} job.",
        "Return only the JSON object on stdout. Do not use tools or read files.",
        adapt.tagged("TRANSCRIPT", text),
        adapt.tagged("PUNTOS", _puntos_reference(classic)),
    ])
    data = adapt.call_json_agent(
        ctx,
        prompt_path=_AGENTS_DIR / spec["prompt"],
        log_name=f"carrusel_{fmt}_adapt",
        message=message,
    )
    return _validate_adapted(data, spec, classic)


def _adapt_or_classic(ctx, fmt: str, classic: dict) -> dict:
    """Adapted content when the agent delivers; the classic plan otherwise.
    A carousel must never disappear because an agent hiccuped."""
    try:
        return _adapted_slides(ctx, fmt, classic)
    except Exception as exc:  # noqa: BLE001 — spec: any adaptation failure falls back
        print(
            f"[formats] carrusel_{fmt}: adaptador no disponible, uso el plan clasico ({exc})",
            file=sys.stderr, flush=True,
        )
        return classic


def render_carousel_variant(
    ctx, *, component_file: str, prefix: str, w: int, h: int,
    props: dict, n_slides: int,
) -> list[Path]:
    """Render `n_slides` stills (frame i -> slide i) of `component_file` into
    `ctx.out_dir` as `<prefix>_slideNN.png` (NN = 01, 02, …), deleting any stale
    `<prefix>_slide*.png` first. Returns the written paths."""
    if ctx.out_dir is None or n_slides <= 0:
        return []
    out_dir = Path(ctx.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob(f"{prefix}_slide*.png"):
        try:
            stale.unlink()
        except OSError:
            pass
    out_pngs = [out_dir / f"{prefix}_slide{i:02d}.png" for i in range(1, n_slides + 1)]
    render_format_stills(
        composition_id=_COMP_ID,
        component_file=component_file,
        width=w,
        height=h,
        duration_frames=n_slides,
        props=props,
        out_pngs=out_pngs,
        frames=list(range(n_slides)),
    )
    return out_pngs


def _variant(ctx, *, component_file: str, prefix: str, slides: list[dict],
             title: str, eyebrow: str, bg_offset: int = 0) -> list[Path]:
    """Render a standard variant (hero at frame 0, one frame per content slide).

    `bg_offset` selects one randomized full-bleed background from the b-roll
    library (varied per run); when the library is empty the comp falls back to
    its flat palette background."""
    props = {
        "title": title,
        "eyebrow": eyebrow,
        "slides": slides,
        "palette": ctx.palette(),
    }
    props.update(_background_props(ctx, bg_offset))
    return render_carousel_variant(
        ctx, component_file=component_file, prefix=prefix,
        w=CAROUSEL_WIDTH, h=CAROUSEL_HEIGHT, props=props, n_slides=len(slides) + 1,
    )


# --- Task 4.2/4.3: layout variants (shared props) -----------------------------

def produce_numeros(ctx) -> list[Path]:
    """Big-number listicle (01/02/03…), agent-adapted from the transcript."""
    data = classic_slides(ctx)
    if data is None:
        return []
    data = _adapt_or_classic(ctx, "numeros", data)
    slides = list(data["slides"]) + [_cta_slide("numeros")]
    return _variant(
        ctx, component_file="formats/CarouselNumeros", prefix="carrusel_numeros",
        slides=slides, title=data["title"], eyebrow=data["eyebrow"],
        bg_offset=_BG_OFFSETS["numeros"],
    )


def produce_checklist(ctx) -> list[Path]:
    """Checklist layout (agent-adapted into real checkable actions); appends a
    final "Guarda este post" CTA slide."""
    data = classic_slides(ctx)
    if data is None:
        return []
    data = _adapt_or_classic(ctx, "checklist", data)
    slides = list(data["slides"]) + [
        {"kind": "cta", "heading": "Guarda este post",
         "body": "Vuelve a este cuando lo necesites.", "index": 0, "total": 0}
    ]
    return _variant(
        ctx, component_file="formats/CarouselChecklist", prefix="carrusel_checklist",
        slides=slides, title=data["title"], eyebrow=data["eyebrow"],
        bg_offset=_BG_OFFSETS["checklist"],
    )


def produce_editorial(ctx) -> list[Path]:
    """Magazine/editorial layout (serif display, thin rules, folio),
    agent-adapted into a magazine voice."""
    data = classic_slides(ctx)
    if data is None:
        return []
    data = _adapt_or_classic(ctx, "editorial", data)
    slides = list(data["slides"]) + [_cta_slide("editorial")]
    return _variant(
        ctx, component_file="formats/CarouselEditorial", prefix="carrusel_editorial",
        slides=slides, title=data["title"], eyebrow=data["eyebrow"],
        bg_offset=_BG_OFFSETS["editorial"],
    )


def produce_citas(ctx) -> list[Path]:
    """One quote per slide (oversized, centered), refit for standalone visuals
    by the shared frases adapter (falls back to the raw quote texts)."""
    quotes = [q for q in frase_adapt.adapted_citas(ctx) if _clean(q)]
    if not quotes:
        return []
    cover = classic_slides(ctx) or {}
    title = cover.get("title") or "En sus palabras"
    eyebrow = cover.get("eyebrow") or "Frases"
    total = len(quotes)
    slides = [
        {"kind": "quote", "body": q, "index": i, "total": total}
        for i, q in enumerate(quotes, start=1)
    ] + [_cta_slide("citas")]
    return _variant(
        ctx, component_file="formats/CarouselCitas", prefix="carrusel_citas",
        slides=slides, title=title, eyebrow=eyebrow,
        bg_offset=_BG_OFFSETS["citas"],
    )


# --- Task 4.4: mitos (myth vs truth, agent-derived pairs) ---------------------

def _parse_mitos(stdout: str) -> list[dict]:
    """Extract `[{"mito","realidad"}]` from the agent's JSON. Raises on any
    failure so the producer can cleanly skip the format."""
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"mitos returned no JSON object: {stdout[:200]!r}")
    data = json.loads(stdout[start:end + 1])
    pares = data.get("pares") if isinstance(data, dict) else None
    out: list[dict] = []
    for p in pares or []:
        if not isinstance(p, dict):
            continue
        mito = _clean(p.get("mito"))
        realidad = _clean(p.get("realidad"))
        if mito and realidad:
            out.append({"mito": mito, "realidad": realidad})
    if not out:
        raise ValueError("mitos returned no usable pairs")
    return out


def _mitos_pairs(ctx, items: list[dict]) -> list[dict]:
    """Ask the mitos agent to reframe each key point as a MITO/REALIDAD pair.

    Returns `[{"mito","realidad"}]`. Raises on any failure (no agent, prompt
    missing, non-zero exit, unparseable output) — `produce_mitos` turns that into
    a clean skip.
    """
    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise RuntimeError(missing_agent_cmd_message())
    if not MITOS_PROMPT_PATH.exists():
        raise RuntimeError(f"mitos system prompt missing: {MITOS_PROMPT_PATH}")
    points_text = "\n".join(
        f"{i}. {it['heading']}" + (f" - {it['body']}" if _clean(it.get("body")) else "")
        for i, it in enumerate(items, start=1)
    )
    message = "\n".join([
        "Run the Mitos job.",
        "Return only the JSON object on stdout. Do not use tools or read files.",
        _tagged("PUNTOS", points_text),
    ])
    log_dir = ctx.run_dir / "logs" / "agent-calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    result = run_agent_code(
        agent_cmd=agent_cmd,
        system_prompt=MITOS_PROMPT_PATH.read_text(encoding="utf-8") + adapt.FIRST_PERSON_VOICE,
        initial_message=message,
        cwd=REPO_ROOT,
        timeout_seconds=MITOS_TIMEOUT_S,
        max_turns=MITOS_MAX_TURNS,
        tools=[],
        model=MITOS_MODEL,
        effort=MITOS_EFFORT,
        permission_mode=None,
        log_path=log_dir / "mitos.log",
    )
    if result.returncode != 0:
        tail = (result.stdout + "\n" + result.stderr).strip()[-300:]
        raise RuntimeError(f"mitos agent failed (exit {result.returncode}); {tail}")
    return _parse_mitos(result.stdout)


def produce_mitos(ctx) -> list[Path]:
    """Myth-vs-truth carousel. Agent failure => skip (return []), never error."""
    data = classic_slides(ctx)
    if data is None:
        return []
    try:
        pairs = _mitos_pairs(ctx, data["slides"])
    except Exception as exc:  # noqa: BLE001 — spec: agent failure skips the format
        print(f"[formats] carrusel_mitos: agente no disponible, se omite ({exc})",
              file=sys.stderr, flush=True)
        return []
    total = len(pairs)
    slides = [
        {"kind": "par", "mito": p["mito"], "realidad": p["realidad"],
         "index": i, "total": total}
        for i, p in enumerate(pairs, start=1)
    ] + [_cta_slide("mitos")]
    return _variant(
        ctx, component_file="formats/CarouselMitos", prefix="carrusel_mitos",
        slides=slides, title=data["title"], eyebrow=data["eyebrow"],
        bg_offset=_BG_OFFSETS["mitos"],
    )


# --- Task 4.7: fotos (full-bleed photo layout) --------------------------------

def _stage_broll_photos(video_id: str, photos: list[Path]) -> list[str]:
    """Copy chosen photos into `remotion/public/assets/runs/<id>/broll/` and
    return the `staticFile`-relative paths. The tree is pruned by the pipeline's
    `_prune_remotion_run_artifacts` after the run."""
    dest_dir = REMOTION_PUBLIC / "assets" / "runs" / str(video_id) / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    for i, src in enumerate(photos, start=1):
        name = f"foto_{i:02d}{src.suffix.lower()}"
        try:
            shutil.copy2(src, dest_dir / name)
        except OSError:
            continue
        rels.append(f"assets/runs/{video_id}/broll/{name}")
    return rels


def _stage_broll_background(video_id: str, bg: Path) -> str | None:
    """Copy one chosen background canvas into
    `remotion/public/assets/runs/<id>/broll/` and return its `staticFile`-relative
    path (or None on copy failure). Named `bg_<source-stem>` so it never collides
    with the `foto_*` photo stagings and identical picks across formats dedupe to
    one file. Pruned by the pipeline's `_prune_remotion_run_artifacts`."""
    dest_dir = REMOTION_PUBLIC / "assets" / "runs" / str(video_id) / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", bg.stem).strip("_") or "bg"
    name = f"bg_{safe}{bg.suffix.lower()}"
    try:
        shutil.copy2(bg, dest_dir / name)
    except OSError:
        return None
    return f"assets/runs/{video_id}/broll/{name}"


def _background_props(ctx, offset: int) -> dict:
    """`{"bgSrc","darkBg"}` staging one randomized background for this carousel,
    or `{}` when the library is empty / staging failed (flat-color fallback). A
    True `darkBg` tells the comp to flip to a LIGHT element/text palette."""
    bg = broll.select_background(ctx, offset=offset)
    if bg is None:
        return {}
    staged = _stage_broll_background(ctx.video_id, bg)
    if not staged:
        return {}
    return {"bgSrc": staged, "darkBg": broll.is_dark_background(bg)}


def produce_fotos(ctx) -> list[Path]:
    """Key points laid over the operator's photos, agent-compressed to
    glanceable over-photo captions. Skips when the b-roll library is empty."""
    data = classic_slides(ctx)
    if data is None:
        return []
    data = _adapt_or_classic(ctx, "fotos", data)
    slides = data["slides"]
    # +1 so the hero COVER gets its own photo, distinct from every content slide
    # (the cover used to reuse slide 1's image).
    photos = broll.select_photos(ctx, len(slides) + 1, "carrusel")
    if not photos:
        return []
    staged = _stage_broll_photos(ctx.video_id, photos)
    if not staged:
        return []
    # Hero = first photo; each content slide takes a DISTINCT later photo, so no
    # slide (cover included) ever reuses another slide's image.
    photo_slides = [
        {**s, "photoSrc": staged[(i + 1) % len(staged)]} for i, s in enumerate(slides)
    ]
    props = {
        "title": data["title"],
        "eyebrow": data["eyebrow"],
        "slides": photo_slides,
        "palette": ctx.palette(),
        "heroPhotoSrc": staged[0],
    }
    return render_carousel_variant(
        ctx, component_file="formats/CarouselFotos", prefix="carrusel_fotos",
        w=CAROUSEL_WIDTH, h=CAROUSEL_HEIGHT, props=props, n_slides=len(slides) + 1,
    )


# --- Task 4.8: ratio variants + PDF -------------------------------------------

def produce_stories(ctx) -> list[Path]:
    """Classic content re-rendered at 1080x1920 (Stories) via `Carousel.tsx`,
    over a randomized background with a follow/save CTA closer."""
    raw = _raw_classic_slides(ctx)
    if not raw:
        return []
    slides = list(raw) + [_cta_slide("stories")]
    w, h = STORIES_SIZE
    props = {"slides": slides, **_background_props(ctx, _BG_OFFSETS["stories"])}
    return render_carousel_variant(
        ctx, component_file="Carousel", prefix="carrusel_stories",
        w=w, h=h, props=props, n_slides=len(slides),
    )


def produce_cuadrado(ctx) -> list[Path]:
    """Classic content re-rendered at 1080x1080 (square) via `Carousel.tsx`,
    over a randomized background with a follow/save CTA closer."""
    raw = _raw_classic_slides(ctx)
    if not raw:
        return []
    slides = list(raw) + [_cta_slide("cuadrado")]
    w, h = SQUARE_SIZE
    props = {"slides": slides, **_background_props(ctx, _BG_OFFSETS["cuadrado"])}
    return render_carousel_variant(
        ctx, component_file="Carousel", prefix="carrusel_cuadrado",
        w=w, h=h, props=props, n_slides=len(slides),
    )


def produce_linkedin_pdf(ctx) -> list[Path]:
    """Assemble the published classic slides into a single multi-page PDF
    (LinkedIn document). Returns [] when no classic slides are present."""
    if ctx.out_dir is None:
        return []
    out_dir = Path(ctx.out_dir)
    pngs = _published_classic_pngs(out_dir)
    if not pngs:
        return []
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow guarded; degrade to skip
        return []
    images = []
    for p in pngs:
        try:
            images.append(Image.open(p).convert("RGB"))
        except Exception:  # noqa: BLE001 — a corrupt slide never fails the PDF
            continue
    if not images:
        return []
    pdf_path = out_dir / "carrusel_linkedin.pdf"
    try:
        images[0].save(pdf_path, "PDF", save_all=True, append_images=images[1:])
    finally:
        for im in images:
            im.close()
    return [pdf_path]
