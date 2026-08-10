import json
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import registry, runner


def _spec(key, category, kind, requires=()):
    return registry.FormatSpec(
        key=key, category=category, label=f"L {key}",
        producer=f"contenido_bionico.short.formats.fake:{key}",
        requires=requires, outputs=(f"{key}.out",), kind=kind,
    )


def _make_producer(*names):
    def producer(ctx):
        paths = []
        for name in names:
            p = ctx.out_dir / name
            p.write_bytes(b"x")
            paths.append(p)
        return paths
    return producer


@pytest.fixture
def env(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ctx = runner.FormatContext(video_id="1_short", run_dir=run_dir, out_dir=out_dir)
    monkeypatch.setattr(runner.FormatContext, "for_run", classmethod(lambda cls, vid: ctx))
    monkeypatch.setattr(runner.broll, "list_photos", lambda: [])  # no b-roll by default
    producers: dict = {}
    monkeypatch.setattr(runner.registry, "resolve_producer", lambda spec: producers[spec.key])

    def set_formats(specs):
        monkeypatch.setattr(runner.registry, "FORMATS", {s.key: s for s in specs})

    return SimpleNamespace(
        ctx=ctx, run_dir=run_dir, out_dir=out_dir,
        producers=producers, set_formats=set_formats,
    )


def test_disabled_category_toggle_skips_producer(env):
    calls = []

    def prod(ctx):
        calls.append("clip")
        p = ctx.out_dir / "clip.mp4"
        p.write_bytes(b"x")
        return [p]

    env.producers["clip"] = prod
    env.set_formats([_spec("clip", "videos", "video")])
    out = runner.run_formats_stage("1_short", toggles={"videos_extra": False})
    assert calls == []
    assert out == []
    assert not (env.out_dir / "clip.mp4").exists()


def test_missing_required_atom_skips_without_error(env):
    calls = []
    env.producers["q"] = lambda ctx: calls.append("q") or []
    env.set_formats([_spec("q", "citas", "quote", requires=("quote_plan",))])
    out = runner.run_formats_stage("1_short")  # run_dir has no quotes/Quote_Plan.json
    assert calls == []
    assert out == []
    assert not (env.run_dir / "logs" / "formats" / "q.log").exists()


def test_one_producer_raises_others_still_run_and_log(env):
    def boom(ctx):
        raise RuntimeError("kaboom")

    env.producers["bad"] = boom
    env.producers["good"] = _make_producer("good.png")
    env.set_formats([
        _spec("bad", "videos", "video"),
        _spec("good", "imagenes", "image"),
    ])
    out = runner.run_formats_stage("1_short")
    assert [p.name for p in out] == ["good.png"]
    assert (env.out_dir / "good.png").exists()
    log = env.run_dir / "logs" / "formats" / "bad.log"
    assert log.exists()
    assert "kaboom" in log.read_text(encoding="utf-8")


def test_manifest_lists_only_produced(env):
    env.producers["good"] = _make_producer("good.mp4")
    env.set_formats([_spec("good", "videos", "video")])
    runner.run_formats_stage("1_short")
    manifest = json.loads((env.out_dir / "formats_manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["video_id"] == "1_short"
    assert manifest["categories"]["videos"] == [
        {"file": "good.mp4", "kind": "video", "label": "L good", "format": "good"}
    ]


def test_no_pack_zips_written(env):
    # Packs are zipped ON DEMAND by the cloud Worker now; the stage never writes
    # pack_*.zip (uploading them alongside the individual files doubled the upload).
    env.producers["v"] = _make_producer("v.mp4")
    env.producers["i"] = _make_producer("i.png")
    env.set_formats([_spec("v", "videos", "video"), _spec("i", "imagenes", "image")])
    runner.run_formats_stage("1_short")
    assert not list(env.out_dir.glob("pack_*.zip"))


def test_shipping_deliverables_indexed(env):
    (env.out_dir / "final_1.mp4").write_bytes(b"f")
    (env.out_dir / "caption.txt").write_bytes(b"c")
    env.producers["srt"] = _make_producer("subtitulos.srt")
    env.set_formats([_spec("srt", "textos", "caption")])
    runner.run_formats_stage("1_short")
    manifest = json.loads((env.out_dir / "formats_manifest.json").read_text(encoding="utf-8"))
    videos = {e["file"] for e in manifest["categories"]["videos"]}
    assert "final_1.mp4" in videos
    textos = {e["file"] for e in manifest["categories"]["textos"]}
    assert {"subtitulos.srt", "caption.txt"} <= textos
    assert not list(env.out_dir.glob("pack_*.zip"))


def test_no_output_dir_returns_empty(env, monkeypatch):
    ctx = runner.FormatContext(video_id="1_short", run_dir=env.run_dir, out_dir=None)
    monkeypatch.setattr(runner.FormatContext, "for_run", classmethod(lambda cls, vid: ctx))
    env.set_formats([_spec("v", "videos", "video")])
    env.producers["v"] = _make_producer("v.mp4")
    assert runner.run_formats_stage("1_short") == []
