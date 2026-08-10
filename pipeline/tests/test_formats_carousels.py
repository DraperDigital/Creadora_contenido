"""WS4 carousel-variant tests: adapter shape, variant-render plumbing (naming,
frame math, stale deletion), producer prop-building + skip paths, the V6.1
per-format adaptation agents (success mapping, cap enforcement, fallback to
classic), mitos JSON parsing, and LinkedIn PDF assembly. Render-heavy paths are
covered by the live smoke run (documented in the WS4 report), not here."""
from __future__ import annotations

from pathlib import Path

import pytest

from contenido_bionico.short.formats import adapt, carousels

# --- fixtures -----------------------------------------------------------------

CAROUSEL = {
    "title": "Cómo vender más",
    "slides": [
        {"kind": "hero", "eyebrow": "El sistema", "title": "Cómo vender más", "footnote": "3 claves"},
        {"kind": "item", "index": 1, "total": 3, "heading": "El alcance no paga",
         "body": "Tener seguidores no sirve si ninguno te compra."},
        {"kind": "item", "index": 2, "total": 3, "heading": "Deja de regalar todo",
         "body": "Si enseñas gratis lo que deberías cobrar, no te pagan."},
        {"kind": "item", "index": 3, "total": 3, "heading": "Construye un sistema", "body": ""},
    ],
}

QUOTES = {"quotes": [
    {"text": "El alcance no paga las facturas."},
    {"text": "Deja de regalar tu mejor material."},
    {"text": "Un sistema vende por ti mientras duermes."},
]}

TRANSCRIPT = {"words": [
    {"type": "word", "text": w}
    for w in "hola en este video te cuento como vender mas sin regalar tu trabajo".split()
]}


class FakeCtx:
    """Minimal stand-in for FormatContext: the producers only touch these."""

    def __init__(self, tmp: Path, *, carousel=None, quotes=None, palette=None,
                 transcript=None, video_id="9104_short"):
        self.video_id = video_id
        self.run_dir = tmp / "run"
        self.out_dir = tmp / "out"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._carousel = carousel
        self._quotes = quotes
        self._transcript = transcript
        self._palette = palette or {
            "bg": "#1A1712", "ink": "#231D17", "paper": "#F6F1E7",
            "accent": "#E8724C", "accentSoft": "#F4B69E",
        }

    def carousel_plan(self):
        return self._carousel

    def quote_plan(self):
        return self._quotes

    def palette(self):
        return dict(self._palette)

    def transcript(self):
        return self._transcript


@pytest.fixture(autouse=True)
def _no_real_agent(monkeypatch):
    """Unit tests must never reach a real agent CLI: the shared transport seam
    raises unless a test stubs it explicitly (producers then fall back to the
    classic plan, which is the asserted behavior of the passthrough tests)."""
    def _forbidden(*args, **kwargs):
        raise adapt.AdaptError("unit tests must stub call_json_agent")

    monkeypatch.setattr(carousels.adapt, "call_json_agent", _forbidden)
    # frase_adapt binds call_json_agent by name; guard its seam too.
    monkeypatch.setattr(carousels.frase_adapt, "_call_agent", _forbidden)
    carousels.frase_adapt._reset_memo()
    yield
    carousels.frase_adapt._reset_memo()


@pytest.fixture(autouse=True)
def _no_backgrounds(monkeypatch):
    """Static carousels randomize a real full-bleed background from
    broll_library/backgrounds/ when that folder is populated; unit tests default
    to the empty-library path (flat palette colors) so the prop assertions stay
    deterministic. Background-specific tests override this stub."""
    monkeypatch.setattr(carousels.broll, "select_background",
                        lambda ctx, *, offset=0: None)


@pytest.fixture
def fake_render(monkeypatch):
    """Replace the real Remotion still render with a recorder that creates the
    output PNGs so callers see files on disk without invoking a render."""
    calls: list[dict] = []

    def _fake(**kw):
        calls.append(kw)
        for p in kw["out_pngs"]:
            Path(p).write_bytes(b"\x89PNG\r\n")
        return list(kw["out_pngs"])

    monkeypatch.setattr(carousels, "render_format_stills", _fake)
    return calls


def _patch_adapt(monkeypatch, payload=None, error=None):
    """Stub the shared `adapt.call_json_agent` seam; returns the recorded calls."""
    calls: list[dict] = []

    def fake(ctx, *, prompt_path, log_name, message, **kw):
        calls.append({"prompt_path": Path(prompt_path), "log_name": log_name,
                      "message": message})
        if error is not None:
            raise error
        return payload

    monkeypatch.setattr(carousels.adapt, "call_json_agent", fake)
    return calls


# --- classic_slides adapter ---------------------------------------------------

def test_classic_slides_shape(tmp_path):
    data = carousels.classic_slides(FakeCtx(tmp_path, carousel=CAROUSEL))
    assert data["title"] == "Cómo vender más"
    assert data["eyebrow"] == "El sistema"
    assert len(data["slides"]) == 3
    assert data["slides"][0] == {
        "kind": "item", "heading": "El alcance no paga",
        "body": "Tener seguidores no sirve si ninguno te compra.",
        "index": 1, "total": 3,
    }
    assert data["slides"][2]["body"] == ""  # empty body preserved


def test_classic_slides_none_without_plan(tmp_path):
    assert carousels.classic_slides(FakeCtx(tmp_path, carousel=None)) is None


def test_classic_slides_none_without_items(tmp_path):
    ctx = FakeCtx(tmp_path, carousel={"title": "x", "slides": [{"kind": "hero", "title": "x"}]})
    assert carousels.classic_slides(ctx) is None


def test_classic_slides_falls_back_to_default_eyebrow(tmp_path):
    plan = {"title": "T", "slides": [
        {"kind": "hero", "title": "T"},
        {"kind": "item", "heading": "H", "body": "B", "index": 1, "total": 1},
    ]}
    data = carousels.classic_slides(FakeCtx(tmp_path, carousel=plan))
    assert data["eyebrow"] == carousels.DEFAULT_EYEBROW


# --- render_carousel_variant plumbing -----------------------------------------

def test_render_variant_names_and_frames(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.render_carousel_variant(
        ctx, component_file="formats/CarouselNumeros", prefix="carrusel_numeros",
        w=1080, h=1350, props={"a": 1}, n_slides=4,
    )
    assert [p.name for p in out] == [
        "carrusel_numeros_slide01.png", "carrusel_numeros_slide02.png",
        "carrusel_numeros_slide03.png", "carrusel_numeros_slide04.png",
    ]
    kw = fake_render[0]
    assert kw["frames"] == [0, 1, 2, 3]
    assert kw["duration_frames"] == 4
    assert (kw["width"], kw["height"]) == (1080, 1350)
    assert kw["component_file"] == "formats/CarouselNumeros"
    assert all(p.exists() for p in out)


def test_render_variant_deletes_stale(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    stale = ctx.out_dir / "carrusel_numeros_slide09.png"
    stale.write_bytes(b"old")
    out = carousels.render_carousel_variant(
        ctx, component_file="formats/CarouselNumeros", prefix="carrusel_numeros",
        w=1080, h=1350, props={}, n_slides=2,
    )
    assert not stale.exists()
    assert len(out) == 2


# --- producers: prop building + counts ----------------------------------------
# (No transcript on these ctxs and the agent seam raises, so the adapted
# producers exercise their fallback: the classic plan must flow through intact.)

def test_produce_numeros(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.produce_numeros(ctx)
    assert len(out) == 5  # hero + 3 items + cta
    kw = fake_render[0]
    assert kw["component_file"] == "formats/CarouselNumeros"
    assert kw["props"]["title"] == "Cómo vender más"
    assert kw["props"]["slides"][:-1] == carousels.classic_slides(ctx)["slides"]
    assert kw["props"]["slides"][-1]["kind"] == "cta"
    assert set(kw["props"]["palette"]) == {"bg", "ink", "paper", "accent", "accentSoft"}


def test_produce_checklist_appends_cta(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.produce_checklist(ctx)
    slides = fake_render[0]["props"]["slides"]
    assert slides[-1]["kind"] == "cta"
    assert slides[-1]["heading"] == "Guarda este post"
    assert len(out) == 5  # hero + 3 items + cta


def test_produce_editorial(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.produce_editorial(ctx)
    assert fake_render[0]["component_file"] == "formats/CarouselEditorial"
    assert fake_render[0]["props"]["slides"][-1]["kind"] == "cta"
    assert len(out) == 5  # hero + 3 items + cta


def test_produce_citas_from_quotes(tmp_path, fake_render):
    # frases adapter unavailable (no transcript) => raw quote texts flow through.
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, quotes=QUOTES)
    out = carousels.produce_citas(ctx)
    slides = fake_render[0]["props"]["slides"]
    assert [s["kind"] for s in slides] == ["quote", "quote", "quote", "cta"]
    assert slides[0]["body"] == "El alcance no paga las facturas."
    assert len(out) == 5  # hero + 3 quotes + cta


def test_produce_citas_skips_without_quotes(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, quotes={"quotes": []})
    assert carousels.produce_citas(ctx) == []


def test_produce_citas_works_without_carousel(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=None, quotes=QUOTES)
    out = carousels.produce_citas(ctx)
    assert len(out) == 5  # hero + 3 quotes + cta
    assert fake_render[0]["props"]["title"]  # a fallback title is present


def test_produce_stories_uses_classic_comp_1080x1920(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.produce_stories(ctx)
    kw = fake_render[0]
    assert kw["component_file"] == "Carousel"
    assert (kw["width"], kw["height"]) == (1080, 1920)
    assert kw["props"]["slides"][:-1] == CAROUSEL["slides"]  # raw slides incl. hero
    assert kw["props"]["slides"][-1]["kind"] == "cta"  # follow/save closer appended
    assert [p.name for p in out][0] == "carrusel_stories_slide01.png"
    assert len(out) == 5  # raw slides + cta


def test_produce_cuadrado_1080x1080(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.produce_cuadrado(ctx)
    kw = fake_render[0]
    assert (kw["width"], kw["height"]) == (1080, 1080)
    assert kw["props"]["slides"][-1]["kind"] == "cta"  # follow/save closer appended
    assert [p.name for p in out][0] == "carrusel_cuadrado_slide01.png"
    assert len(out) == 5  # raw slides + cta


def test_producers_skip_without_plan(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=None)
    for fn in (carousels.produce_numeros, carousels.produce_checklist,
               carousels.produce_editorial, carousels.produce_stories,
               carousels.produce_cuadrado):
        assert fn(ctx) == []


# --- V6.1 per-format adaptation agents ------------------------------------------

ADAPTED = {
    "title": "Tu checklist para vender más",
    "eyebrow": "Checklist",
    "slides": [
        {"heading": "Define tu oferta primero", "body": "Sin algo que cobrar, el alcance no sirve."},
        {"heading": "Cobra tu mejor material", "body": "Regalarlo entrena a tu audiencia a no pagarte."},
        {"heading": "Monta un sistema de venta", "body": ""},
        {"heading": "Deja de perseguir seguidores", "body": "Persigue compradores."},
    ],
}


def test_produce_checklist_uses_adapted_content(tmp_path, fake_render, monkeypatch):
    calls = _patch_adapt(monkeypatch, payload=ADAPTED)
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    out = carousels.produce_checklist(ctx)
    props = fake_render[0]["props"]
    assert props["title"] == "Tu checklist para vender más"
    assert props["eyebrow"] == "Checklist"
    slides = props["slides"]
    assert slides[0] == {"kind": "item", "heading": "Define tu oferta primero",
                         "body": "Sin algo que cobrar, el alcance no sirve.",
                         "index": 1, "total": 4}
    assert [s["heading"] for s in slides[:-1]] == [s["heading"] for s in ADAPTED["slides"]]
    assert slides[-1]["kind"] == "cta"  # CTA still appended after adapted items
    assert len(out) == 6  # hero + 4 adapted + cta
    # The agent got the transcript as source and the classic plan as grounding.
    call = calls[0]
    assert call["log_name"] == "carrusel_checklist_adapt"
    assert call["prompt_path"].name == "carrusel_checklist.md"
    assert "<TRANSCRIPT>" in call["message"] and "<PUNTOS>" in call["message"]
    assert "como vender mas" in call["message"]          # transcript text
    assert "El alcance no paga" in call["message"]        # classic point


def test_produce_numeros_uses_adapted_content(tmp_path, fake_render, monkeypatch):
    calls = _patch_adapt(monkeypatch, payload=ADAPTED)
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    out = carousels.produce_numeros(ctx)
    assert calls[0]["prompt_path"].name == "carrusel_numeros.md"
    assert calls[0]["log_name"] == "carrusel_numeros_adapt"
    assert len(fake_render[0]["props"]["slides"]) == 5  # 4 adapted items + cta
    assert fake_render[0]["props"]["slides"][-1]["kind"] == "cta"
    assert len(out) == 6  # hero + 4 adapted items + cta


def test_produce_fotos_uses_adapted_content(tmp_path, fake_render, monkeypatch):
    _patch_adapt(monkeypatch, payload=ADAPTED)
    seen = {}

    def _select(ctx, n, purpose):
        seen["n"] = n
        return [Path("uno.png")]

    monkeypatch.setattr(carousels.broll, "select_photos", _select)
    monkeypatch.setattr(carousels, "_stage_broll_photos",
                        lambda vid, photos: ["assets/runs/x/broll/foto_01.png"])
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    out = carousels.produce_fotos(ctx)
    props = fake_render[0]["props"]
    assert seen["n"] == 5  # ADAPTED slide count (4) + 1 so the hero cover gets its own photo
    assert [s["heading"] for s in props["slides"]] == [s["heading"] for s in ADAPTED["slides"]]
    assert all(s["photoSrc"] for s in props["slides"])
    assert len(out) == 5  # hero + 4 adapted items


def test_adapted_caps_enforced_by_truncation(tmp_path, fake_render, monkeypatch):
    long_heading = "palabra " * 30            # ~240 chars, must cut at a word
    long_body = "detalle importante " * 20    # ~380 chars
    payload = {"title": "t " * 90, "eyebrow": "kicker demasiado largo para la fila superior",
               "slides": [
                   {"heading": long_heading, "body": long_body},
                   {"heading": "", "body": "sin heading: se descarta"},
                   {"heading": "Dos", "body": ""},
                   {"heading": "Tres", "body": ""},
                   {"heading": "Cuatro", "body": ""},
               ]}
    _patch_adapt(monkeypatch, payload=payload)
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    carousels.produce_numeros(ctx)
    props = fake_render[0]["props"]
    assert len(props["title"]) <= carousels.ADAPT_TITLE_MAX
    assert len(props["eyebrow"]) <= carousels.ADAPT_EYEBROW_MAX
    assert props["slides"][-1]["kind"] == "cta"
    slides = props["slides"][:-1]  # drop the appended cta closer
    assert len(slides) == 4  # empty-heading slide dropped
    assert len(slides[0]["heading"]) <= 36 and not slides[0]["heading"].endswith(" ")
    assert len(slides[0]["body"]) <= 110
    assert [s["index"] for s in slides] == [1, 2, 3, 4]  # re-indexed after drop
    assert all(s["total"] == 4 for s in slides)


def test_adapted_fotos_word_caps():
    payload = {"title": "Titulo", "eyebrow": "E", "slides": [
        {"heading": "uno dos tres cuatro cinco seis siete ocho",
         "body": " ".join(f"w{i}" for i in range(20))},
        {"heading": "a", "body": ""},
        {"heading": "b", "body": ""},
        {"heading": "c", "body": ""},
    ]}
    classic = {"title": "x", "eyebrow": "y", "slides": []}
    data = carousels._validate_adapted(payload, carousels.ADAPT_SPECS["fotos"], classic)
    assert data["slides"][0]["heading"] == "uno dos tres cuatro cinco seis"  # 6 words
    assert len(data["slides"][0]["body"].split()) == 12


def test_adapted_trims_to_max_slides(tmp_path, fake_render, monkeypatch):
    payload = {"title": "T", "eyebrow": "E",
               "slides": [{"heading": f"Punto {i}", "body": ""} for i in range(1, 10)]}
    _patch_adapt(monkeypatch, payload=payload)
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    carousels.produce_editorial(ctx)
    assert fake_render[0]["props"]["slides"][-1]["kind"] == "cta"
    slides = fake_render[0]["props"]["slides"][:-1]  # drop the appended cta closer
    assert len(slides) == carousels.ADAPT_MAX_SLIDES
    assert all(s["total"] == carousels.ADAPT_MAX_SLIDES for s in slides)


def test_adapted_falls_back_to_classic_on_agent_error(tmp_path, fake_render, monkeypatch):
    _patch_adapt(monkeypatch, error=adapt.AdaptError("boom"))
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    out = carousels.produce_numeros(ctx)
    assert fake_render[0]["props"]["slides"][:-1] == carousels.classic_slides(ctx)["slides"]
    assert fake_render[0]["props"]["slides"][-1]["kind"] == "cta"
    assert len(out) == 5  # carousel still ships (hero + 3 items + cta)


def test_adapted_falls_back_when_payload_unusable(tmp_path, fake_render, monkeypatch):
    # 2 usable slides < ADAPT_MIN_SLIDES => classic passthrough.
    _patch_adapt(monkeypatch, payload={"title": "T", "eyebrow": "E", "slides": [
        {"heading": "uno", "body": ""}, {"heading": "dos", "body": ""}]})
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, transcript=TRANSCRIPT)
    carousels.produce_checklist(ctx)
    slides = fake_render[0]["props"]["slides"]
    assert [s["heading"] for s in slides[:-1]] == [
        s["heading"] for s in carousels.classic_slides(ctx)["slides"]]


def test_adapted_falls_back_without_transcript(tmp_path, fake_render, monkeypatch):
    calls = _patch_adapt(monkeypatch, payload=ADAPTED)
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)  # transcript() -> None
    carousels.produce_editorial(ctx)
    assert calls == []  # the agent is never invoked without a transcript
    assert fake_render[0]["props"]["slides"][:-1] == carousels.classic_slides(ctx)["slides"]
    assert fake_render[0]["props"]["slides"][-1]["kind"] == "cta"


def test_adapt_prompt_files_exist():
    for spec in carousels.ADAPT_SPECS.values():
        assert (carousels._AGENTS_DIR / spec["prompt"]).is_file()


def test_produce_citas_uses_adapted_texts(tmp_path, fake_render, monkeypatch):
    monkeypatch.setattr(carousels.frase_adapt, "adapted_citas",
                        lambda ctx: ["Cita adaptada uno.", "Cita adaptada dos."])
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, quotes=QUOTES)
    out = carousels.produce_citas(ctx)
    slides = fake_render[0]["props"]["slides"]
    assert [s["body"] for s in slides[:-1]] == ["Cita adaptada uno.", "Cita adaptada dos."]
    assert slides[-1]["kind"] == "cta"
    assert len(out) == 4  # hero + 2 adapted quotes + cta


def test_mitos_upgraded_to_opus_medium():
    assert carousels.MITOS_MODEL == "claude-opus-4-8"
    assert carousels.MITOS_EFFORT == "medium"
    assert carousels.MITOS_TIMEOUT_S >= 300


# --- mitos --------------------------------------------------------------------

def test_produce_mitos_builds_pairs(tmp_path, fake_render, monkeypatch):
    monkeypatch.setattr(carousels, "_mitos_pairs", lambda ctx, items: [
        {"mito": "Más seguidores = más ventas", "realidad": "El alcance no paga"},
        {"mito": "Enseñar gratis fideliza", "realidad": "Deja de regalar todo"},
    ])
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    out = carousels.produce_mitos(ctx)
    slides = fake_render[0]["props"]["slides"]
    assert slides[0]["kind"] == "par"
    assert slides[0]["mito"] and slides[0]["realidad"]
    assert slides[-1]["kind"] == "cta"
    assert len(out) == 4  # hero + 2 pairs + cta


def test_produce_mitos_skips_on_agent_failure(tmp_path, fake_render, monkeypatch):
    def _boom(ctx, items):
        raise RuntimeError("no agent configured")

    monkeypatch.setattr(carousels, "_mitos_pairs", _boom)
    assert carousels.produce_mitos(FakeCtx(tmp_path, carousel=CAROUSEL)) == []


def test_parse_mitos_extracts_pairs():
    pairs = carousels._parse_mitos(
        'prose {"pares":[{"mito":"a","realidad":"b"},{"mito":"c","realidad":"d"}]} tail'
    )
    assert pairs == [{"mito": "a", "realidad": "b"}, {"mito": "c", "realidad": "d"}]


def test_parse_mitos_raises_without_json():
    with pytest.raises(Exception):
        carousels._parse_mitos("no json here")


def test_parse_mitos_raises_when_all_pairs_empty():
    with pytest.raises(Exception):
        carousels._parse_mitos('{"pares":[{"mito":"","realidad":""}]}')


# --- fotos --------------------------------------------------------------------

def test_produce_fotos_skips_without_photos(tmp_path, fake_render, monkeypatch):
    monkeypatch.setattr(carousels.broll, "select_photos", lambda ctx, n, purpose: [])
    assert carousels.produce_fotos(FakeCtx(tmp_path, carousel=CAROUSEL)) == []


# --- LinkedIn PDF -------------------------------------------------------------

def _png(path: Path, size=(48, 60), color=(200, 100, 50)):
    from PIL import Image
    Image.new("RGB", size, color).save(path)


def test_linkedin_pdf_assembles(tmp_path):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    for i in range(1, 4):
        _png(ctx.out_dir / f"carrusel_slide{i}.png")
    out = carousels.produce_linkedin_pdf(ctx)
    assert len(out) == 1
    pdf = out[0]
    assert pdf.name == "carrusel_linkedin.pdf"
    assert pdf.exists() and pdf.stat().st_size > 0
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_linkedin_pdf_sorts_numerically(tmp_path):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    for i in range(1, 12):  # 1..11 — exposes lexicographic slide10 < slide2 bug
        _png(ctx.out_dir / f"carrusel_slide{i}.png")
    ordered = carousels._published_classic_pngs(ctx.out_dir)
    import re
    nums = [int(re.search(r"(\d+)", p.stem).group(1)) for p in ordered]
    assert nums == list(range(1, 12))


def test_linkedin_pdf_skips_when_none(tmp_path):
    assert carousels.produce_linkedin_pdf(FakeCtx(tmp_path, carousel=CAROUSEL)) == []


# --- randomized backgrounds + palette flip ------------------------------------

def _stub_background(monkeypatch, name, staged_rel):
    """Force one background pick + a fixed staged path (no real fs copy)."""
    bg = Path(name)
    monkeypatch.setattr(carousels.broll, "select_background",
                        lambda ctx, *, offset=0: bg)
    monkeypatch.setattr(carousels, "_stage_broll_background",
                        lambda vid, path: staged_rel)
    return bg


def test_variant_without_backgrounds_has_no_bg_props(tmp_path, fake_render):
    # Default (empty library via the _no_backgrounds stub): flat-color fallback,
    # so no bgSrc/darkBg reach the comp.
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    carousels.produce_numeros(ctx)
    props = fake_render[0]["props"]
    assert "bgSrc" not in props and "darkBg" not in props


def test_variant_dark_background_flips_to_light_palette(tmp_path, fake_render, monkeypatch):
    _stub_background(monkeypatch, "canvas_dark.png", "assets/runs/x/broll/bg_canvas_dark.png")
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    carousels.produce_numeros(ctx)
    props = fake_render[0]["props"]
    assert props["bgSrc"] == "assets/runs/x/broll/bg_canvas_dark.png"
    assert props["darkBg"] is True  # "dark" in filename => light element/text flip


def test_variant_light_background_keeps_dark_palette(tmp_path, fake_render, monkeypatch):
    _stub_background(monkeypatch, "paper_light.png", "assets/runs/x/broll/bg_paper_light.png")
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    carousels.produce_editorial(ctx)
    props = fake_render[0]["props"]
    assert props["bgSrc"] == "assets/runs/x/broll/bg_paper_light.png"
    assert props["darkBg"] is False  # light canvas keeps the dark element/text palette


def test_stories_threads_background(tmp_path, fake_render, monkeypatch):
    _stub_background(monkeypatch, "night_dark.png", "assets/runs/x/broll/bg_night_dark.png")
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    carousels.produce_stories(ctx)
    props = fake_render[0]["props"]
    assert props["bgSrc"] == "assets/runs/x/broll/bg_night_dark.png"
    assert props["darkBg"] is True


def test_background_props_empty_when_no_library(tmp_path):
    # _no_backgrounds autouse stub -> select_background returns None -> {}.
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    assert carousels._background_props(ctx, 0) == {}


def test_stage_broll_background_copies_and_namespaces(tmp_path, monkeypatch):
    monkeypatch.setattr(carousels, "REMOTION_PUBLIC", tmp_path / "public")
    src = tmp_path / "Mi Fondo Dark.png"
    src.write_bytes(b"\x89PNG\r\n")
    rel = carousels._stage_broll_background("9104_short", src)
    assert rel == "assets/runs/9104_short/broll/bg_Mi_Fondo_Dark.png"
    dest = (tmp_path / "public" / "assets" / "runs" / "9104_short" / "broll"
            / "bg_Mi_Fondo_Dark.png")
    assert dest.exists() and dest.read_bytes() == b"\x89PNG\r\n"


# --- CTA closers (item 9) -----------------------------------------------------

def test_static_carousels_append_cta(tmp_path, fake_render):
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL, quotes=QUOTES)
    for fn in (carousels.produce_numeros, carousels.produce_editorial,
               carousels.produce_citas, carousels.produce_stories,
               carousels.produce_cuadrado):
        fake_render.clear()
        fn(ctx)
        cta = fake_render[0]["props"]["slides"][-1]
        assert cta["kind"] == "cta"
        assert cta["heading"] and cta["body"]  # a real Spanish follow/save closer


def test_mitos_appends_cta(tmp_path, fake_render, monkeypatch):
    monkeypatch.setattr(carousels, "_mitos_pairs", lambda ctx, items: [
        {"mito": "a", "realidad": "b"}, {"mito": "c", "realidad": "d"}])
    ctx = FakeCtx(tmp_path, carousel=CAROUSEL)
    carousels.produce_mitos(ctx)
    cta = fake_render[0]["props"]["slides"][-1]
    assert cta["kind"] == "cta" and cta["heading"]
