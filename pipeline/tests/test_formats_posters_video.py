from types import SimpleNamespace

from contenido_bionico.short.formats import posters_video


def _ctx(tmp_path):
    run, out = tmp_path / "run", tmp_path / "out"
    run.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        video_id="7_short", run_dir=run, out_dir=out,
        palette=lambda: {"bg": "#111", "ink": "#222", "paper": "#eee",
                         "accent": "#f50", "accentSoft": "#fa8"},
        transcript=lambda: {"text": "x"},
    )


def _patch_common(monkeypatch, cap):
    monkeypatch.setattr(posters_video.poster_content, "content",
                        lambda ctx: {"headline": ["Una frase fuerte"], "keywords": [],
                                     "points": [], "quote": "", "summary": ""})
    monkeypatch.setattr(posters_video.posters, "_stage", lambda ctx, p: (str(p) if p else None))
    monkeypatch.setattr(posters_video, "_poster_video_audio", lambda ctx, s, w: w / "a.m4a")
    monkeypatch.setattr(posters_video._vc, "_mux_audio", lambda v, a, out: out)

    def fake_video(**kw):
        kw["out_mp4"].parent.mkdir(parents=True, exist_ok=True)
        kw["out_mp4"].write_bytes(b"v")
        cap.append(kw)
        return kw["out_mp4"]

    monkeypatch.setattr(posters_video, "render_format_video", fake_video)


def test_solid_video_uses_distinct_foreground_per_layout(monkeypatch, tmp_path):
    cap: list = []
    seen: list = []
    _patch_common(monkeypatch, cap)
    monkeypatch.setattr(posters_video.posters, "_solid_subject",
                        lambda ctx, index=0: seen.append(index) or (tmp_path / f"fg{index}.png"))
    out1 = posters_video.produce_1(_ctx(tmp_path))
    out2 = posters_video.produce_2(_ctx(tmp_path))
    assert [p.name for p in out1] == ["poster_video_1.mp4"]
    assert [p.name for p in out2] == ["poster_video_2.mp4"]
    assert cap[0]["component_file"] == "formats/PosterLayout1"
    assert cap[0]["duration_frames"] > 1            # video -> PosterFrame auto-animates
    assert cap[0]["props"]["mode"] == "solid"
    assert seen == [0, 1]                            # layout 1 -> index 0, layout 2 -> index 1


def test_foto_video_uses_distinct_pair_per_layout(monkeypatch, tmp_path):
    cap: list = []
    seen: list = []
    _patch_common(monkeypatch, cap)
    monkeypatch.setattr(
        posters_video.posters, "_foto_pair",
        lambda ctx, index=0: seen.append(index) or (tmp_path / f"bg{index}.png", tmp_path / f"fg{index}.png"),
    )
    out3 = posters_video.produce_3_foto(_ctx(tmp_path))
    assert [p.name for p in out3] == ["poster_video_3_foto.mp4"]
    assert cap[0]["props"]["mode"] == "foto"
    assert cap[0]["props"]["bgSrc"] and cap[0]["props"]["subjectSrc"]
    assert seen == [2]                               # layout 3 -> index 2


def test_foto_video_skips_without_pair(monkeypatch, tmp_path):
    cap: list = []
    _patch_common(monkeypatch, cap)
    monkeypatch.setattr(posters_video.posters, "_foto_pair", lambda ctx, index=0: None)
    assert posters_video.produce_1_foto(_ctx(tmp_path)) == []
    assert cap == []                                 # nothing rendered


def test_produce_poster_video_no_content_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(posters_video.poster_content, "content", lambda ctx: None)
    assert posters_video.produce_1(_ctx(tmp_path)) == []
    assert posters_video.produce_1_foto(_ctx(tmp_path)) == []
