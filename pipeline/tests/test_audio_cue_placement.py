"""Unit tests for SFX cue placement basics.

Covers the pure pieces of the audio plan build: segment-timing extraction,
cue offset clamping / spacing / per-segment cap in ``validate_cues``, and the
segment-relative -> absolute-seconds anchoring in ``build_audio_plan``.
Manifests are constructed in-memory with tmp files; no ffprobe, no library.
"""
import json

import pytest

from contenido_bionico.shared.audio import build_audio_plan as bap
from contenido_bionico.shared.audio.manifest import (
    Constraints,
    Manifest,
    MusicConfig,
    SfxEntry,
)
from contenido_bionico.shared.audio.validate_cues import resolve_cues_payload


def _manifest(tmp_path, *, max_cues=40, spacing=0.05):
    sfx_dir = tmp_path / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    (sfx_dir / "pop.wav").write_bytes(b"x")
    (sfx_dir / "slide.wav").write_bytes(b"x")
    entries = {
        "pop": SfxEntry(
            name="pop", file="sfx/pop.wav", category="ui",
            duration_seconds=0.4, default_gain_db=-12.0,
        ),
        "slide": SfxEntry(
            name="slide", file="sfx/slide.wav", category="ui",
            duration_seconds=0.6, default_gain_db=-14.0,
        ),
    }
    return Manifest(
        version=1,
        library_root=tmp_path,
        sfx_by_name=entries,
        music=MusicConfig(
            directory="music", loop_strategy="crossfade_loop",
            crossfade_seconds=2.0, base_gain_db=-18.0,
        ),
        constraints=Constraints(
            max_cues_per_segment=max_cues, min_cue_spacing_seconds=spacing,
        ),
    )


def _payload(cues):
    return {"segment_id": 0, "cues": cues}


# ---------- _segment_time_start_map ----------

def test_segment_time_map_extracts_valid_and_skips_invalid():
    assembly = {
        "segments": [
            {"segment_id": 0, "time_start": 0.0, "time_end": 2.0},
            {"segment_id": 3, "time_start": 12.5, "time_end": 15.0},
            {"segment_id": "bad", "time_start": 1.0, "time_end": 2.0},
            {"segment_id": 4, "time_start": None, "time_end": 2.0},
        ]
    }
    assert bap._segment_time_start_map(assembly) == {
        0: (0.0, 2.0),
        3: (12.5, 15.0),
    }


def test_segment_time_map_empty_assembly():
    assert bap._segment_time_start_map({}) == {}


# ---------- offset clamping ----------

def test_negative_offset_clamps_to_zero(tmp_path):
    manifest = _manifest(tmp_path)
    res = resolve_cues_payload(
        _payload([{"sfx_name": "pop", "offset_seconds": -0.5}]),
        manifest,
        segment_duration_seconds=3.0,
    )
    assert len(res.resolved) == 1
    assert res.resolved[0].offset_seconds == 0.0
    assert any("clamped to 0" in w for w in res.warnings)


def test_offset_beyond_duration_clamps_to_segment_end(tmp_path):
    manifest = _manifest(tmp_path)
    res = resolve_cues_payload(
        _payload([{"sfx_name": "pop", "offset_seconds": 9.9}]),
        manifest,
        segment_duration_seconds=3.0,
    )
    assert res.resolved[0].offset_seconds == 3.0
    assert any("clamped to segment end" in w for w in res.warnings)


def test_in_range_offset_and_manifest_gain_pass_through(tmp_path):
    manifest = _manifest(tmp_path)
    res = resolve_cues_payload(
        _payload([{"sfx_name": "pop", "offset_seconds": 1.25}]),
        manifest,
        segment_duration_seconds=3.0,
    )
    cue = res.resolved[0]
    assert cue.offset_seconds == 1.25
    assert cue.gain_db == -12.0  # from the manifest, not the cue
    assert cue.match_type == "exact_name"


# ---------- spacing + per-segment cap ----------

def test_cues_closer_than_min_spacing_drop_the_later_one(tmp_path):
    manifest = _manifest(tmp_path, spacing=0.5)
    res = resolve_cues_payload(
        _payload(
            [
                {"sfx_name": "pop", "offset_seconds": 1.0},
                {"sfx_name": "slide", "offset_seconds": 1.2},
                {"sfx_name": "pop", "offset_seconds": 2.0},
            ]
        ),
        manifest,
        segment_duration_seconds=5.0,
    )
    assert [c.offset_seconds for c in res.resolved] == [1.0, 2.0]
    assert any("spacing below" in w for w in res.warnings)


def test_max_cues_per_segment_keeps_earliest(tmp_path):
    manifest = _manifest(tmp_path, max_cues=2, spacing=0.0)
    res = resolve_cues_payload(
        _payload(
            [
                {"sfx_name": "pop", "offset_seconds": 3.0},
                {"sfx_name": "slide", "offset_seconds": 1.0},
                {"sfx_name": "pop", "offset_seconds": 2.0},
            ]
        ),
        manifest,
        segment_duration_seconds=5.0,
    )
    assert [c.offset_seconds for c in res.resolved] == [1.0, 2.0]
    assert any("exceeds max" in w for w in res.warnings)


def test_unknown_sound_is_reported_not_fatal(tmp_path):
    manifest = _manifest(tmp_path)
    res = resolve_cues_payload(
        _payload([{"requested_sound": "zzz qqq www", "offset_seconds": 0.5}]),
        manifest,
        segment_duration_seconds=3.0,
    )
    assert res.resolved == ()
    assert len(res.unresolved) == 1
    assert res.unresolved[0].reason == "no confident catalog match"


# ---------- absolute anchoring in build_audio_plan ----------

def test_resolve_sound_cues_anchors_offsets_to_segment_start(tmp_path):
    manifest = _manifest(tmp_path)
    run_dir = tmp_path / "9_short"
    seg_dir = run_dir / "animations" / "3"
    seg_dir.mkdir(parents=True)
    (seg_dir / "Sound_Cues.json").write_text(
        json.dumps(
            {
                "segment_id": 3,
                "cues": [{"sfx_name": "pop", "offset_seconds": 1.25}],
            }
        ),
        encoding="utf-8",
    )
    resolved, unresolved, warnings = bap._resolve_sound_cues(
        run_dir=run_dir,
        manifest=manifest,
        segment_id=3,
        time_start=12.5,
        time_end=15.0,
    )
    assert unresolved == [] and warnings == []
    assert len(resolved) == 1
    cue = resolved[0]
    assert cue["absolute_seconds"] == pytest.approx(13.75)
    assert cue["segment_id"] == 3
    assert cue["sfx_name"] == "pop"
    assert cue["kind"] == "author_selected"


def test_resolve_sound_cues_missing_file_is_silent_noop(tmp_path):
    manifest = _manifest(tmp_path)
    run_dir = tmp_path / "9_short"
    (run_dir / "animations" / "0").mkdir(parents=True)
    assert bap._resolve_sound_cues(
        run_dir=run_dir,
        manifest=manifest,
        segment_id=0,
        time_start=0.0,
        time_end=2.0,
    ) == ([], [], [])


def test_build_audio_plan_sorts_cues_across_segments(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    run_dir = tmp_path / "9_short"
    run_dir.mkdir()
    (run_dir / "source.mp4").write_bytes(b"x")
    (run_dir / "Assembly_Instructions.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"segment_id": 0, "time_start": 0.5, "time_end": 4.0},
                    {"segment_id": 1, "time_start": 10.0, "time_end": 14.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    for seg_id, offset in ((0, 2.0), (1, 0.25)):
        seg_dir = run_dir / "animations" / str(seg_id)
        seg_dir.mkdir(parents=True)
        (seg_dir / "Sound_Cues.json").write_text(
            json.dumps(
                {
                    "segment_id": seg_id,
                    "cues": [{"sfx_name": "pop", "offset_seconds": offset}],
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(bap, "load_manifest", lambda root, verify_files=False: manifest)
    monkeypatch.setattr(bap, "_probe_duration", lambda p: 20.0)
    monkeypatch.setattr(bap, "_pick_music_file", lambda m, vid, dur: None)

    out_path = bap.build_audio_plan(run_dir, tmp_path)
    plan = json.loads(out_path.read_text(encoding="utf-8"))
    assert [c["absolute_seconds"] for c in plan["sfx_cues"]] == [2.5, 10.25]
    assert [c["segment_id"] for c in plan["sfx_cues"]] == [0, 1]
    assert plan["unresolved_cues"] == []
