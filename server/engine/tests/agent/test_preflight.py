import pytest

from bionico.agent import preflight as pf


@pytest.fixture(autouse=True)
def _tools_present(monkeypatch):
    # prepare_input() skips everything when ffmpeg/ffprobe are missing;
    # pretend both exist and stub the probe/remux per test.
    monkeypatch.setattr(pf.shutil, "which", lambda name: "C:/fake/" + name)


def _mp4(tmp_path, name="in.mp4"):
    p = tmp_path / name
    p.write_bytes(b"fake video bytes")
    return p


def test_ok_vertical_mp4_passes_through(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "probe", lambda p: (60.0, 1080, 1920))
    src = _mp4(tmp_path)
    assert pf.prepare_input(src) == src


def test_corrupt_after_redownload_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "probe", lambda p: None)
    attempts = []
    src = _mp4(tmp_path)
    with pytest.raises(pf.PreflightRejected) as e:
        pf.prepare_input(src, redownload=lambda: attempts.append(1))
    assert attempts == [1]  # exactly ONE re-download attempt
    assert "dañado" in e.value.reason


def test_corrupt_then_valid_redownload_recovers(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky_probe(p):
        calls["n"] += 1
        return None if calls["n"] == 1 else (30.0, 720, 1280)

    monkeypatch.setattr(pf, "probe", flaky_probe)
    src = _mp4(tmp_path)
    assert pf.prepare_input(src, redownload=lambda: None) == src


def test_too_long_rejects_with_duration_message(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "probe", lambda p: (500.0, 1080, 1920))
    with pytest.raises(pf.PreflightRejected) as e:
        pf.prepare_input(_mp4(tmp_path), max_seconds=420)
    assert "máximo permitido es 420" in e.value.reason


def test_horizontal_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "probe", lambda p: (60.0, 1920, 1080))
    with pytest.raises(pf.PreflightRejected) as e:
        pf.prepare_input(_mp4(tmp_path))
    assert "horizontal" in e.value.reason


def test_mov_gets_remuxed_to_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "probe", lambda p: (60.0, 1080, 1920))
    out_holder = {}

    def fake_remux(src):
        out = src.parent / (src.stem + ".mp4")
        out.write_bytes(b"remuxed")
        out_holder["out"] = out
        return out

    monkeypatch.setattr(pf, "remux_to_mp4", fake_remux)
    src = tmp_path / "in.mov"
    src.write_bytes(b"mov bytes")
    assert pf.prepare_input(src) == out_holder["out"]


def test_missing_tools_hands_file_through(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)
    src = tmp_path / "in.mov"
    src.write_bytes(b"mov bytes")
    out = pf.prepare_input(src)
    assert out.suffix == ".mp4"  # renamed best-effort, never blocks the job


def test_rotation_swaps_display_dimensions():
    stream = {"codec_type": "video", "width": 1920, "height": 1080,
              "side_data_list": [{"rotation": -90}]}
    assert pf._rotation_of(stream) % 180 == 90
