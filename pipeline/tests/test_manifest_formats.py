from pathlib import Path

from contenido_bionico.shared import remotion_manifest_formats as rmf


def test_generated_ts_contains_id_dims_duration_props(tmp_path):
    comp = tmp_path / "Compositions.abc.ts"
    rmf.write_format_composition_ts(
        comp,
        composition_id="typing-carousel",
        component_file="formats/TypingCarousel",
        width=1080,
        height=1920,
        duration_frames=240,
        props={"title": "Canción", "slides": [{"heading": "Envío rápido"}]},
    )
    text = comp.read_text(encoding="utf-8")
    assert 'id: "typing-carousel"' in text
    assert "width: 1080" in text
    assert "height: 1920" in text
    assert "durationInFrames: 240" in text
    assert 'import Comp from "./formats/TypingCarousel";' in text
    assert "component: Comp," in text
    assert "export const COMPOSITIONS" in text
    # props embedded as JSON with ensure_ascii=False (accents stay literal)
    assert '"title": "Canción"' in text
    assert '"heading": "Envío rápido"' in text
    assert "\\u" not in text


def test_render_format_video_uses_fast_opaque(monkeypatch, tmp_path):
    captured = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return kwargs["out_webm"]

    monkeypatch.setattr(rmf, "render_isolated_composition", fake_render)
    out = rmf.render_format_video(
        composition_id="c", component_file="formats/Foo",
        width=1080, height=1920, duration_frames=90, props={},
        out_mp4=tmp_path / "o.mp4",
    )
    assert out == tmp_path / "o.mp4"
    assert captured["fast_opaque"] is True
    assert captured["require_alpha"] is False
    assert captured["composition_id"] == "c"
    # the write callback produces the expected TS when invoked
    comp = tmp_path / "Compositions.x.ts"
    captured["write_compositions"](comp)
    assert 'import Comp from "./formats/Foo";' in comp.read_text(encoding="utf-8")


def test_render_format_overlay_requires_alpha(monkeypatch, tmp_path):
    captured = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return kwargs["out_webm"]

    monkeypatch.setattr(rmf, "render_isolated_composition", fake_render)
    rmf.render_format_overlay(
        composition_id="c", component_file="formats/Bar",
        width=1080, height=960, duration_frames=90, props={},
        out_webm=tmp_path / "o.webm",
    )
    assert captured["require_alpha"] is True
    assert captured.get("fast_opaque") in (None, False)


def test_render_format_stills_passes_frames(monkeypatch, tmp_path):
    captured = {}

    def fake_stills(**kwargs):
        captured.update(kwargs)
        return kwargs["out_pngs"]

    monkeypatch.setattr(rmf, "render_isolated_stills", fake_stills)
    pngs = [tmp_path / "a.png", tmp_path / "b.png"]
    out = rmf.render_format_stills(
        composition_id="c", component_file="formats/Baz",
        width=1080, height=1350, duration_frames=2, props={},
        out_pngs=pngs, frames=[0, 1],
    )
    assert out == pngs
    assert captured["frames"] == [0, 1]
    assert captured["out_pngs"] == pngs
