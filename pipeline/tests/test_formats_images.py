"""Tests for the static-image producers (WS5, V6.1 LLM-fitted texts).

Renders are mocked (`render_format_stills` monkeypatched to write a dummy PNG
and record its props) and so are the adaptation seams (the `frase_adapt`
accessors and `adapt.call_json_agent`), so these assert the selection/staging/
skip logic, the adapted-text consumption, the infografia cap enforcement, and
the props each producer feeds its composition — not pixels.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import adapt as adapt_mod
from contenido_bionico.short.formats import images

_NOTSET = object()


@pytest.fixture
def env(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "quotes").mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stage = tmp_path / "stage"
    calls: list = []
    agent_calls: list = []

    def fake_render(*, composition_id, component_file, width, height, duration_frames, props, out_pngs, frames=None):
        assert (width, height) == (1080, 1350)
        for p in out_pngs:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_bytes(b"PNGDATA")
        calls.append(
            SimpleNamespace(
                composition_id=composition_id,
                component_file=component_file,
                props=props,
                out=[Path(p) for p in out_pngs],
                frames=frames,
            )
        )
        return list(out_pngs)

    monkeypatch.setattr(images, "render_format_stills", fake_render)
    monkeypatch.setattr(images, "_broll_stage_dir", lambda vid: stage)

    # Adaptation seams: never touch the real agent in unit tests. Defaults
    # mimic frase_adapt's raw-quote fallback; tests override via _set_adapt /
    # _set_infografia_agent.
    def _raw(ctx):
        plan = ctx.quote_plan() or {}
        out = []
        for q in plan.get("quotes") or []:
            text = str((q.get("text") if isinstance(q, dict) else q) or "").strip()
            if text:
                out.append(text)
        return out

    monkeypatch.setattr(images.frase_adapt, "adapted_citas", _raw)
    monkeypatch.setattr(images.frase_adapt, "adapted_frase", lambda ctx: (_raw(ctx) or [None])[0])
    monkeypatch.setattr(
        images.frase_adapt,
        "hero_word",
        lambda ctx: (_raw(ctx)[0].split()[0].upper() if _raw(ctx) else None),
    )
    monkeypatch.setattr(
        images.frase_adapt,
        "main_idea",
        lambda ctx: (" ".join(_raw(ctx)[0].split()[:7]) if _raw(ctx) else None),
    )
    # Default: no pre-made cutout pair, so frase_detras uses the masthead path.
    # Scene-path tests override this with an explicit (background, foreground).
    monkeypatch.setattr(images.broll, "select_cutout_pair", lambda ctx: None)

    def failing_agent(ctx, **kwargs):
        agent_calls.append(kwargs)
        raise adapt_mod.AdaptError("no agent in unit tests")

    monkeypatch.setattr(images.adapt, "call_json_agent", failing_agent)

    def make_ctx(quotes=None, carousel=None, transcript="hola este es el guion"):
        palette = {
            "bg": "#000000", "ink": "#111111", "paper": "#ffffff",
            "accent": "#ff0000", "accentSoft": "#ff8888",
        }
        words = (
            {"words": [{"type": "word", "text": w} for w in transcript.split()]}
            if transcript
            else None
        )
        return SimpleNamespace(
            video_id="5_short",
            run_dir=run_dir,
            out_dir=out_dir,
            quote_plan=lambda: ({"quotes": [{"text": q} for q in quotes]} if quotes is not None else None),
            carousel_plan=lambda: carousel,
            transcript=lambda: words,
            palette=lambda: palette,
        )

    return SimpleNamespace(
        run_dir=run_dir, out_dir=out_dir, stage=stage,
        calls=calls, agent_calls=agent_calls,
        make_ctx=make_ctx, monkeypatch=monkeypatch,
    )


def _photos(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / f"ph{i}.jpg"
        p.write_bytes(b"J" * 4096)
        out.append(p)
    return out


def _set_photos(env, photos):
    env.monkeypatch.setattr(images.broll, "select_photos", lambda ctx, n, purpose: list(photos)[:n])


def _set_adapt(env, citas=_NOTSET, frase=_NOTSET, palabra=_NOTSET, idea=_NOTSET):
    """Override the frase_adapt accessors with fixed returns (None is valid)."""
    if citas is not _NOTSET:
        env.monkeypatch.setattr(images.frase_adapt, "adapted_citas", lambda ctx: list(citas))
    if frase is not _NOTSET:
        env.monkeypatch.setattr(images.frase_adapt, "adapted_frase", lambda ctx: frase)
    if palabra is not _NOTSET:
        env.monkeypatch.setattr(images.frase_adapt, "hero_word", lambda ctx: palabra)
    if idea is not _NOTSET:
        env.monkeypatch.setattr(images.frase_adapt, "main_idea", lambda ctx: idea)


def _set_cutout_pair(env, background, foreground):
    """Make `broll.select_cutout_pair` return a fixed (background, foreground)."""
    env.monkeypatch.setattr(images.broll, "select_cutout_pair", lambda ctx: (background, foreground))


def _set_infografia_agent(env, payload):
    """Make `adapt.call_json_agent` return `payload` (recording the message)."""
    def ok_agent(ctx, **kwargs):
        env.agent_calls.append(kwargs)
        return payload

    env.monkeypatch.setattr(images.adapt, "call_json_agent", ok_agent)


# --- cita_estatica (rendered quote cards) --------------------------------------


def test_cita_estatica_renders_adapted_quote_cards(env):
    _set_adapt(env, citas=["Cita uno fit", "Cita dos fit"])
    ctx = env.make_ctx(quotes=["a", "b"])
    out = images.produce_cita_estatica(ctx)
    assert [p.name for p in out] == ["cita_1.png", "cita_2.png"]
    assert len(env.calls) == 2
    first = env.calls[0]
    assert first.component_file == "formats/FraseImagen"
    assert first.props["style"] == "plantilla"
    assert first.props["text"] == "Cita uno fit"
    assert first.props["eyebrow"] == "CITA"
    assert env.calls[1].props["text"] == "Cita dos fit"


def test_cita_estatica_renders_instead_of_copying(env):
    # A pre-rendered quote still exists but must NOT be copied any more.
    (env.run_dir / "quotes" / "quote_1.png").write_bytes(b"old-copy-bytes")
    ctx = env.make_ctx(quotes=["a"])
    out = images.produce_cita_estatica(ctx)
    assert [p.name for p in out] == ["cita_1.png"]
    assert (env.out_dir / "cita_1.png").read_bytes() == b"PNGDATA"  # rendered
    assert len(env.calls) == 1


def test_cita_estatica_clears_stale_but_not_cita_foto(env):
    (env.out_dir / "cita_9.png").write_bytes(b"stale")
    (env.out_dir / "cita_foto_1.png").write_bytes(b"keep")
    ctx = env.make_ctx(quotes=["a"])
    images.produce_cita_estatica(ctx)
    assert not (env.out_dir / "cita_9.png").exists()
    assert (env.out_dir / "cita_foto_1.png").exists()  # different format, untouched


def test_cita_estatica_no_quotes_returns_empty(env):
    ctx = env.make_ctx(quotes=[])
    assert images.produce_cita_estatica(ctx) == []
    assert env.calls == []


# --- cita_foto ----------------------------------------------------------------


def test_cita_foto_pairs_adapted_texts_and_photos(env, tmp_path):
    _set_photos(env, _photos(tmp_path, 3))
    _set_adapt(env, citas=["Fit 1", "Fit 2", "Fit 3", "Fit 4"])
    ctx = env.make_ctx(quotes=["q1", "q2", "q3", "q4"])
    out = images.produce_cita_foto(ctx)
    assert [p.name for p in out] == ["cita_foto_1.png", "cita_foto_2.png", "cita_foto_3.png"]
    first = env.calls[0]
    assert first.component_file == "formats/FraseImagen"
    assert first.props["style"] == "foto"
    assert first.props["text"] == "Fit 1"
    assert first.props["photoSrc"].startswith("assets/runs/5_short/broll/")
    assert [c.props["text"] for c in env.calls] == ["Fit 1", "Fit 2", "Fit 3"]


def test_cita_foto_zip_truncates_to_fewer_photos(env, tmp_path):
    _set_photos(env, _photos(tmp_path, 2))
    ctx = env.make_ctx(quotes=["q1", "q2", "q3"])
    out = images.produce_cita_foto(ctx)
    assert [p.name for p in out] == ["cita_foto_1.png", "cita_foto_2.png"]


def test_cita_foto_no_photos_returns_empty(env):
    _set_photos(env, [])
    ctx = env.make_ctx(quotes=["q1"])
    assert images.produce_cita_foto(ctx) == []


# --- citas_3en1 ---------------------------------------------------------------


def test_citas_3en1_uses_first_three_adapted(env):
    _set_adapt(env, citas=["c1", "c2", "c3", "c4"])
    ctx = env.make_ctx(quotes=["q1", "q2", "q3", "q4"])
    out = images.produce_citas_3en1(ctx)
    assert [p.name for p in out] == ["citas_3en1.png"]
    assert env.calls[0].component_file == "formats/CitasTresEnUno"
    assert env.calls[0].props["quotes"] == ["c1", "c2", "c3"]


def test_citas_3en1_one_quote_skips(env):
    ctx = env.make_ctx(quotes=["only"])
    assert images.produce_citas_3en1(ctx) == []
    assert env.calls == []


# --- infografia ---------------------------------------------------------------


def _carousel(points, title="Titulo", eyebrow="Guia"):
    slides = [{"kind": "hero", "title": title, "eyebrow": eyebrow}]
    for i, (h, b) in enumerate(points, start=1):
        slides.append({"kind": "item", "index": i, "heading": h, "body": b})
    return {"title": title, "slides": slides}


def test_infografia_agent_payload_capped_three_variants(env):
    _set_infografia_agent(env, {
        "title": "T" * 80,
        "eyebrow": "E" * 40,
        "points": [{"heading": "H" * 50, "body": "B" * 130} for _ in range(7)],
    })
    ctx = env.make_ctx(carousel=_carousel([("H1", "B1"), ("H2", "B2"), ("H3", "B3")]))
    out = images.produce_infografia(ctx)
    assert [p.name for p in out] == ["infografia_1.png", "infografia_2.png", "infografia_3.png"]
    assert [c.props["variant"] for c in env.calls] == [1, 2, 3]
    props = env.calls[0].props
    assert len(props["title"]) <= images.INFOGRAFIA_TITLE_MAX
    assert len(props["eyebrow"]) <= images.INFOGRAFIA_EYEBROW_MAX
    assert len(props["points"]) == images.INFOGRAFIA_MAX_POINTS
    for p in props["points"]:
        assert len(p["heading"]) <= images.INFOGRAFIA_HEADING_MAX
        assert len(p["body"]) <= images.INFOGRAFIA_BODY_MAX
    # The agent got the transcript and the mechanical points as grounding.
    message = env.agent_calls[0]["message"]
    assert "<TRANSCRIPT>" in message and "<PUNTOS>" in message
    assert "1. H1 - B1" in message


def test_infografia_agent_failure_falls_back_bounded(env):
    # Default agent seam raises -> bounded mechanical extraction must be used.
    long_points = [(f"Heading {i} " + "x" * 60, f"Body {i} " + "y" * 120) for i in range(1, 8)]
    ctx = env.make_ctx(carousel=_carousel(long_points, title="T" * 90, eyebrow="E" * 40))
    out = images.produce_infografia(ctx)
    assert len(out) == 3
    props = env.calls[0].props
    assert len(props["title"]) <= images.INFOGRAFIA_TITLE_MAX
    assert len(props["eyebrow"]) <= images.INFOGRAFIA_EYEBROW_MAX
    assert len(props["points"]) == images.INFOGRAFIA_MAX_POINTS  # 7 -> sliced to 5
    for p in props["points"]:
        assert len(p["heading"]) <= images.INFOGRAFIA_HEADING_MAX
        assert len(p["body"]) <= images.INFOGRAFIA_BODY_MAX


def test_infografia_agent_too_few_points_falls_back(env):
    _set_infografia_agent(env, {
        "title": "Titulo del agente",
        "eyebrow": "Agente",
        "points": [{"heading": "Solo uno", "body": ""}],
    })
    ctx = env.make_ctx(carousel=_carousel([("H1", "B1"), ("H2", "B2")]))
    images.produce_infografia(ctx)
    assert env.calls[0].props["title"] == "Titulo"  # mechanical, not the agent's
    assert env.calls[0].props["points"] == [
        {"heading": "H1", "body": "B1"},
        {"heading": "H2", "body": "B2"},
    ]


def test_infografia_no_transcript_skips_agent_uses_fallback(env):
    _set_infografia_agent(env, {
        "title": "No debe usarse",
        "eyebrow": "X",
        "points": [{"heading": f"P{i}", "body": ""} for i in range(3)],
    })
    ctx = env.make_ctx(carousel=_carousel([("H1", "B1")]), transcript=None)
    out = images.produce_infografia(ctx)
    assert len(out) == 3
    assert env.agent_calls == []  # never called without a transcript
    assert env.calls[0].props["title"] == "Titulo"


def test_infografia_no_points_skips(env):
    ctx = env.make_ctx(carousel={"title": "T", "slides": [{"kind": "hero", "title": "T"}]})
    assert images.produce_infografia(ctx) == []


# --- frase_solida / frase_plantilla ------------------------------------------


def test_frase_solida_renders_adapted_frase(env):
    _set_adapt(env, frase="La frase poster")
    ctx = env.make_ctx(quotes=["strong", "weaker"])
    out = images.produce_frase_solida(ctx)
    assert [p.name for p in out] == ["frase_solida.png"]
    assert env.calls[0].props["style"] == "solida"
    assert env.calls[0].props["text"] == "La frase poster"


def test_frase_plantilla_renders_adapted_frase(env):
    _set_adapt(env, frase="La frase poster")
    ctx = env.make_ctx(quotes=["strong"])
    out = images.produce_frase_plantilla(ctx)
    assert [p.name for p in out] == ["frase_plantilla.png"]
    assert env.calls[0].props["style"] == "plantilla"
    assert env.calls[0].props["text"] == "La frase poster"


def test_frase_solida_no_frase_skips(env):
    _set_adapt(env, frase=None)
    ctx = env.make_ctx(quotes=[])
    assert images.produce_frase_solida(ctx) == []
    assert env.calls == []


# --- frase_foto ---------------------------------------------------------------


def test_frase_foto_stages_first_photo(env, tmp_path):
    _set_photos(env, _photos(tmp_path, 2))
    _set_adapt(env, frase="Frase para foto")
    ctx = env.make_ctx(quotes=["strong"])
    out = images.produce_frase_foto(ctx)
    assert [p.name for p in out] == ["frase_foto.png"]
    assert env.calls[0].props["style"] == "foto"
    assert env.calls[0].props["text"] == "Frase para foto"
    assert env.calls[0].props["photoSrc"].startswith("assets/runs/5_short/broll/")


def test_frase_foto_no_photos_skips(env):
    _set_photos(env, [])
    ctx = env.make_ctx(quotes=["strong"])
    assert images.produce_frase_foto(ctx) == []


def test_frase_foto_no_frase_skips(env, tmp_path):
    _set_photos(env, _photos(tmp_path, 1))
    _set_adapt(env, frase=None)
    ctx = env.make_ctx(quotes=["strong"])
    assert images.produce_frase_foto(ctx) == []
    assert env.calls == []


# frase_detras was removed (superseded by the poster formats); its tests went
# with it. Poster producers are covered by test_formats_posters.py.
