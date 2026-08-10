"""V6 multi-format feature toggles through the watcher (sidecar -> CLI flags).

Mirrors the existing sidecar-flag tests: each of the five new keys
(videos_extra, video_carruseles, carruseles_extra, imagenes, textos) follows
the exact music -> --no-music pattern, for fresh uploads (sidecar json) and
for video change tasks (features ride task_args).
"""
import json

from bionico.watcher import watcher


def test_feature_flags_args_each_new_key_false_emits_its_flag():
    assert watcher.feature_flags_args({"videos_extra": False}, None) == ["--no-videos-extra"]
    assert watcher.feature_flags_args({"video_carruseles": False}, None) == ["--no-video-carruseles"]
    assert watcher.feature_flags_args({"carruseles_extra": False}, None) == ["--no-carruseles-extra"]
    assert watcher.feature_flags_args({"imagenes": False}, None) == ["--no-imagenes"]
    assert watcher.feature_flags_args({"textos": False}, None) == ["--no-textos"]


def test_feature_flags_args_new_keys_true_or_missing_emit_nothing():
    assert watcher.feature_flags_args(
        {"videos_extra": True, "video_carruseles": True, "carruseles_extra": True,
         "imagenes": True, "textos": True}, None) == []
    assert watcher.feature_flags_args({}, None) == []


def test_feature_flags_args_all_twelve_false_exact_order():
    # Every toggle off: the --no flags emit in _FEATURE_FLAGS order — the seven
    # legacy flags first, then the five V6 format flags, then anim_count.
    assert watcher.feature_flags_args(
        {"captions": False, "camera": False, "animations": False,
         "music": False, "sfx": False, "carousel": False, "quotes": False,
         "videos_extra": False, "video_carruseles": False,
         "carruseles_extra": False, "imagenes": False, "textos": False}, "max") == [
        "--no-captions", "--no-camera", "--no-animations",
        "--no-music", "--no-sfx", "--no-carousel", "--no-quotes",
        "--no-videos-extra", "--no-video-carruseles", "--no-carruseles-extra",
        "--no-imagenes", "--no-textos",
        "--anim-count", "max"]


def test_sidecar_features_includes_new_format_keys(tmp_path):
    # The new keys survive the nested-"features" read like the other toggles.
    p = tmp_path / "formats.json"
    p.write_text(json.dumps({"features": {
        "videos_extra": False, "video_carruseles": True, "carruseles_extra": False,
        "imagenes": True, "textos": False,
    }}), encoding="utf-8")
    assert watcher._sidecar_features(p) == {
        "videos_extra": False, "video_carruseles": True, "carruseles_extra": False,
        "imagenes": True, "textos": False,
    }


def test_mp4_enqueue_with_format_features_off_nested_in_features(tmp_path):
    # Format toggles off must reach the CLI as their --no flags from the
    # agent's nested "features" sidecar shape (same contract as music/sfx).
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "vidfmt.mp4"
    mp4.write_bytes(b"BYTES-FMT")
    (inbox / "vidfmt.json").write_text(json.dumps(
        {"features": {"videos_extra": False, "textos": False}}), encoding="utf-8")
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidfmt"]
    assert rec["args"] == [str(mp4), "--short", "--no-videos-extra", "--no-textos"]


def test_mp4_enqueue_all_format_features_on_has_no_args(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "vidon.mp4").write_bytes(b"BYTES-ON")
    (inbox / "vidon.json").write_text(json.dumps({"features": {
        "videos_extra": True, "video_carruseles": True, "carruseles_extra": True,
        "imagenes": True, "textos": True,
    }}), encoding="utf-8")
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    assert "args" not in led["bionico/vidon"]


def test_task_args_video_with_format_features_appends_flags():
    # A video change task carries the version's feature snapshot; the new
    # format keys ride task_args exactly like music/sfx do.
    task = {"kind": "edit", "target": "video", "run_id": "7_short",
            "instructions": "make it blue", "id": "j1", "source": "cloud",
            "features": {"imagenes": False, "textos": False}}
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue",
        "--no-imagenes", "--no-textos"]


def test_task_args_carousel_with_format_features_does_not_append_flags():
    task = {"kind": "edit", "target": "carousel", "run_id": "7_short",
            "instructions": "make it blue", "id": "j1", "source": "cloud",
            "features": {"imagenes": False}}
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "carousel", "--notes=make it blue"]
