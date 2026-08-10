from types import SimpleNamespace

from contenido_bionico.short.formats.formats_srt import cues_to_srt, produce_srt


def test_srt_format():
    srt = cues_to_srt([{"text": "Hola mundo", "start": 0.0, "end": 1.25},
                       {"text": "Segunda línea", "start": 1.5, "end": 3.0}])
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,250\nHola mundo\n\n2\n")
    assert "00:00:01,500 --> 00:00:03,000" in srt


def test_srt_hours_and_ms():
    srt = cues_to_srt([{"text": "x", "start": 3661.007, "end": 3661.5}])
    assert "01:01:01,007 --> 01:01:01,500" in srt


def test_produce_srt_writes_file(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir).mkdir()
    (run_dir / "captions_props.json").write_text(
        '{"cues": [{"text": "Hola", "start": 0.0, "end": 1.0}]}', encoding="utf-8"
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ctx = SimpleNamespace(run_dir=run_dir, out_dir=out_dir)
    paths = produce_srt(ctx)
    assert paths == [out_dir / "subtitulos.srt"]
    assert (out_dir / "subtitulos.srt").read_text(encoding="utf-8").startswith("1\n")


def test_produce_srt_no_cues_returns_empty(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ctx = SimpleNamespace(run_dir=run_dir, out_dir=out_dir)
    assert produce_srt(ctx) == []
