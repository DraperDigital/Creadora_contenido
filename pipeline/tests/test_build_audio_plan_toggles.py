"""Unit tests for the include_music / include_sfx gating in build_audio_plan.

Only the IO boundary (manifest load, ffprobe duration, music pick, cue
resolution) is monkeypatched, so these exercise the real gating logic added for
the dashboard's Musica / SFX toggles without needing ffprobe or an audio
library on disk.
"""
import json
from types import SimpleNamespace

import pytest

from contenido_bionico.shared.audio import build_audio_plan as bap


def _setup_run(tmp_path):
    run_dir = tmp_path / "7_short"
    run_dir.mkdir()
    (run_dir / "source.mp4").write_bytes(b"x")
    (run_dir / "Assembly_Instructions.json").write_text(
        json.dumps({"segments": [{"segment_id": 0, "time_start": 0.0, "time_end": 2.0}]}),
        encoding="utf-8",
    )
    return run_dir


def _patch(monkeypatch, tmp_path):
    fake_music = SimpleNamespace(
        base_gain_db=-18.0, loop_strategy="crossfade_loop", crossfade_seconds=2.0
    )
    manifest = SimpleNamespace(
        music=fake_music, library_root=tmp_path, music_dir=lambda: tmp_path / "music"
    )
    monkeypatch.setattr(bap, "load_manifest", lambda root, verify_files=False: manifest)
    monkeypatch.setattr(bap, "_probe_duration", lambda p: 10.0)
    monkeypatch.setattr(bap, "_pick_music_file", lambda m, vid, dur: tmp_path / "music" / "track.mp3")
    # One synthetic resolved cue per segment so include_sfx has an observable effect.
    monkeypatch.setattr(
        bap,
        "_resolve_sound_cues",
        lambda **kw: (
            [{
                "segment_id": kw["segment_id"], "sfx_name": "pop", "file": "x.wav",
                "absolute_seconds": 0.5, "gain_db": -6.0, "kind": "author_selected",
            }],
            [],
            [],
        ),
    )
    return manifest


def test_build_audio_plan_both_layers(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    out = bap.build_audio_plan(_setup_run(tmp_path), tmp_path)
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["music"] is not None
    assert len(plan["sfx_cues"]) == 1


def test_build_audio_plan_no_music_keeps_sfx(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    out = bap.build_audio_plan(_setup_run(tmp_path), tmp_path, include_music=False)
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["music"] is None
    assert len(plan["sfx_cues"]) == 1


def test_build_audio_plan_no_sfx_keeps_music(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    out = bap.build_audio_plan(_setup_run(tmp_path), tmp_path, include_sfx=False)
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["music"] is not None
    assert plan["sfx_cues"] == []


def test_build_audio_plan_music_only_tolerates_no_segments(tmp_path, monkeypatch):
    # A music-only plan (e.g. no-animations + music on) must not require segments.
    _patch(monkeypatch, tmp_path)
    run_dir = _setup_run(tmp_path)
    (run_dir / "Assembly_Instructions.json").write_text(
        json.dumps({"segments": []}), encoding="utf-8"
    )
    out = bap.build_audio_plan(run_dir, tmp_path, include_sfx=False)
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["music"] is not None
    assert plan["sfx_cues"] == []


def test_build_audio_plan_sfx_still_requires_segments(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    run_dir = _setup_run(tmp_path)
    (run_dir / "Assembly_Instructions.json").write_text(
        json.dumps({"segments": []}), encoding="utf-8"
    )
    with pytest.raises(bap.BuildAudioPlanError):
        bap.build_audio_plan(run_dir, tmp_path, include_sfx=True)


# --- music pool selection -----------------------------------------------------

def _music_library(tmp_path, *, shorts=3, no_copyright=3):
    music = tmp_path / "music"
    (music / "shorts").mkdir(parents=True)
    (music / "no-copyright").mkdir(parents=True)
    for i in range(shorts):
        (music / "shorts" / f"short music {i + 1}.mp3").write_bytes(b"s")
    for i in range(no_copyright):
        (music / "no-copyright" / f"no-copyright music {i + 1}.mp3").write_bytes(b"n")
    return SimpleNamespace(library_root=tmp_path, music_dir=lambda: music)


@pytest.mark.parametrize("duration", [12.0, 61.0, 83.0, 600.0])
def test_pick_music_always_from_shorts_pool(tmp_path, duration):
    """Every length draws from shorts/ (the long-form pathway is gone), so 60s+
    shorts no longer leak the no-copyright pool into the final video and every
    format that inherits the Audio_Plan music pick."""
    manifest = _music_library(tmp_path)
    pick = bap._pick_music_file(manifest, "6", duration)
    assert pick is not None
    assert pick.parent.name == "shorts"


def test_pick_music_falls_back_to_no_copyright_when_shorts_empty(tmp_path):
    manifest = _music_library(tmp_path, shorts=0, no_copyright=2)
    pick = bap._pick_music_file(manifest, "6", 83.0)
    assert pick is not None
    assert pick.parent.name == "no-copyright"
