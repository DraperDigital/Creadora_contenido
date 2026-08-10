"""Unit tests for the poster stack: the neutral content author (`poster_content`)
and the 8 layout producers. Agent + staging + renderer are monkeypatched — no
agent CLI, no ffmpeg, no Remotion."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import adapt as adapt_mod
from contenido_bionico.short.formats import poster_content, posters


# --- poster_content -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_memo():
    poster_content._reset_memo()
    yield
    poster_content._reset_memo()


def _content_ctx(tmp_path, transcript="uno dos tres cuatro cinco", carousel=None, quotes=None):
    words = {"words": [{"type": "word", "text": w} for w in transcript.split()]} if transcript else None
    return SimpleNamespace(
        run_dir=tmp_path, video_id="5_short",
        transcript=lambda: words,
        carousel_plan=lambda: carousel,
        quote_plan=lambda: ({"quotes": [{"text": q} for q in quotes]} if quotes else None),
    )


def _patch_content_agent(monkeypatch, payload=None, error=False):
    def fake(ctx, text):
        if error:
            raise adapt_mod.AdaptError("no agent")
        return payload
    monkeypatch.setattr(poster_content, "_call_agent", fake)


def test_content_normalizes_and_caps(tmp_path, monkeypatch):
    _patch_content_agent(monkeypatch, {
        "headline": ["ESTA LINEA ES CLARAMENTE DEMASIADO LARGA", "OK"],
        "keywords": ["UNO", "DOS", "TRES", "CUATRO"],
        "points": [{"heading": "h" * 40, "body": "b" * 200}] * 9,
        "quote": "q" * 300, "summary": "s" * 400,
    })
    c = poster_content.content(_content_ctx(tmp_path))
    # headline is now ONE flat hero phrase (a single-element list), capped in
    # length — the layout balancer breaks it into lines, so we never pre-split.
    assert len(c["headline"]) == 1
    assert all(len(l) <= poster_content.HEADLINE_CHARS for l in c["headline"])
    assert len(c["keywords"]) == 3
    assert len(c["points"]) <= poster_content.MAX_POINTS
    assert len(c["quote"]) <= poster_content.QUOTE_CHARS
    assert len(c["summary"]) <= poster_content.SUMMARY_CHARS


def test_content_is_cached_per_run(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake(ctx, text):
        calls["n"] += 1
        return {"headline": ["A"], "keywords": [], "points": [], "quote": "", "summary": ""}
    monkeypatch.setattr(poster_content, "_call_agent", fake)
    ctx = _content_ctx(tmp_path)
    poster_content.content(ctx)
    poster_content.content(ctx)
    assert calls["n"] == 1  # memoized
    assert (tmp_path / "_intermediates" / poster_content.CACHE_NAME).exists()


def test_content_falls_back_to_carousel(tmp_path, monkeypatch):
    _patch_content_agent(monkeypatch, error=True)
    carousel = {"slides": [
        {"kind": "hero", "title": "Una grabacion muchas piezas"},
        {"kind": "item", "heading": "Graba una vez", "body": "y reutiliza"},
    ]}
    c = poster_content.content(_content_ctx(tmp_path, carousel=carousel))
    assert c is not None and c["headline"]  # derived mechanically
    assert c["points"] and c["points"][0]["heading"] == "Graba una vez"


def test_content_none_when_nothing(tmp_path, monkeypatch):
    _patch_content_agent(monkeypatch, error=True)
    assert poster_content.content(_content_ctx(tmp_path, transcript="", carousel=None, quotes=None)) is None


# --- producers ----------------------------------------------------------------

CONTENT = {"headline": ["UNA", "GRABACION"], "keywords": ["A", "B", "C"],
           "points": [{"heading": "H1", "body": "b1"}], "quote": "q", "summary": "s"}


@pytest.fixture
def penv(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    calls: list = []

    def fake_render(*, composition_id, component_file, width, height, duration_frames, props, out_pngs, frames):
        for p in out_pngs:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"PNG")
        calls.append(SimpleNamespace(composition_id=composition_id, component_file=component_file,
                                     props=props, out=list(out_pngs)))

    monkeypatch.setattr(posters, "render_format_stills", fake_render)
    monkeypatch.setattr(posters, "_stage", lambda ctx, src: (f"assets/{Path(src).name}" if src else None))
    monkeypatch.setattr(posters, "_solid_subject", lambda ctx, index=0: tmp_path / "fg.png")
    monkeypatch.setattr(posters, "_foto_pair", lambda ctx, index=0: (tmp_path / "back.png", tmp_path / "front.png"))
    monkeypatch.setattr(posters.poster_content, "content", lambda ctx: CONTENT)
    ctx = SimpleNamespace(video_id="5_short", run_dir=tmp_path, out_dir=out, palette=lambda: {"accent": "#70f"})
    return SimpleNamespace(calls=calls, ctx=ctx, monkeypatch=monkeypatch)


def test_solid_producer_props(penv):
    out = posters.produce_1(penv.ctx)
    assert [p.name for p in out] == ["poster_1.png"]
    c = penv.calls[0]
    assert c.component_file == "formats/PosterLayout1"
    assert c.props["mode"] == "solid"
    assert c.props["content"] == CONTENT
    assert c.props["subjectSrc"] and "bgSrc" not in c.props


def test_foto_producer_props(penv):
    out = posters.produce_1_foto(penv.ctx)
    assert [p.name for p in out] == ["poster_1_foto.png"]
    c = penv.calls[0]
    assert c.props["mode"] == "foto"
    assert c.props["bgSrc"] and c.props["subjectSrc"]
    assert c.props["bgSrc"] != c.props["subjectSrc"]


def test_foto_skips_without_plate(penv):
    penv.monkeypatch.setattr(posters, "_foto_pair", lambda ctx, index=0: None)
    assert posters.produce_2_foto(penv.ctx) == []
    assert penv.calls == []


def test_producer_skips_without_content(penv):
    penv.monkeypatch.setattr(posters.poster_content, "content", lambda ctx: None)
    assert posters.produce_1(penv.ctx) == []


def test_all_eight_producers_and_layouts(penv):
    for n in (1, 2, 3, 4):
        penv.calls.clear()
        posters.produce_1.__globals__  # noqa: B018 — touch to keep import
        assert getattr(posters, f"produce_{n}")(penv.ctx)[0].name == f"poster_{n}.png"
        assert penv.calls[0].component_file == f"formats/PosterLayout{n}"
        penv.calls.clear()
        assert getattr(posters, f"produce_{n}_foto")(penv.ctx)[0].name == f"poster_{n}_foto.png"


def test_posters_registered():
    from contenido_bionico.short.formats import registry
    keys = {s.key for s in registry.FORMATS.values() if s.category == "posters"}
    expected = {f"poster_{n}{sfx}" for n in (1, 2, 3, 4) for sfx in ("", "_foto")}
    # animated poster mp4s: a SOLID video + a FOTO video per layout
    expected |= {f"poster_video_{n}{sfx}" for n in (1, 2, 3, 4) for sfx in ("", "_foto")}
    assert keys == expected
