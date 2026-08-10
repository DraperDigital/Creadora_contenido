import json
from pathlib import Path

from bionico.watcher import watcher


def _task(target="carousel", run_id="7_short", instructions="make it blue", nonce=1):
    return {"kind": "edit", "target": target, "run_id": run_id,
            "instructions": instructions, "id": "j1", "source": "cloud", "nonce": nonce}


def _write_task(inbox: Path, name: str, task: dict) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    p = inbox / name
    p.write_text(json.dumps(task), encoding="utf-8")
    return p


def test_task_args_mapping():
    assert watcher.task_args(_task("video")) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue"]
    assert watcher.task_args(_task("quotes")) == [
        "--change-run", "7_short", "--target", "quotes", "--notes=make it blue"]
    assert watcher.task_args(_task("carousel")) == [
        "--change-run", "7_short", "--target", "carousel", "--notes=make it blue"]
    assert watcher.task_args({"kind": "edit", "target": "nope"}) is None
    assert watcher.task_args(_task(instructions="")) is None


def test_task_args_video_with_anim_quality_appends_flag():
    task = _task("video")
    task["anim_quality"] = "high"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue",
        "--anim-quality", "high"]


def test_task_args_carousel_with_anim_quality_does_not_append_flag():
    task = _task("carousel")
    task["anim_quality"] = "high"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "carousel", "--notes=make it blue"]


def test_task_args_video_with_invalid_anim_quality_does_not_append_flag():
    task = _task("video")
    task["anim_quality"] = "ultra"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue"]


def test_task_args_video_with_features_and_anim_count_appends_flags():
    task = _task("video")
    task["features"] = {"camera": False, "sfx": False}
    task["anim_count"] = "few"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue",
        "--no-camera", "--no-sfx", "--anim-count", "few"]


def test_task_args_video_with_quality_features_and_anim_count_appends_all():
    task = _task("video")
    task["anim_quality"] = "high"
    task["features"] = {"animations": False}
    task["anim_count"] = "max"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue",
        "--anim-quality", "high", "--no-animations", "--anim-count", "max"]


def test_task_args_carousel_with_features_does_not_append_flags():
    task = _task("carousel")
    task["features"] = {"camera": False}
    task["anim_count"] = "few"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "carousel", "--notes=make it blue"]


def test_task_args_quotes_with_features_does_not_append_flags():
    task = _task("quotes")
    task["features"] = {"camera": False}
    task["anim_count"] = "max"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "quotes", "--notes=make it blue"]


def test_sidecar_quality_valid_and_invalid(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"anim_quality": "mid"}), encoding="utf-8")
    assert watcher._sidecar_quality(valid) == "mid"

    bad_value = tmp_path / "bad_value.json"
    bad_value.write_text(json.dumps({"anim_quality": "ultra"}), encoding="utf-8")
    assert watcher._sidecar_quality(bad_value) is None

    not_json = tmp_path / "not_json.json"
    not_json.write_text("{not json", encoding="utf-8")
    assert watcher._sidecar_quality(not_json) is None

    missing = tmp_path / "missing.json"
    assert watcher._sidecar_quality(missing) is None

    not_a_dict = tmp_path / "not_a_dict.json"
    not_a_dict.write_text(json.dumps(["low"]), encoding="utf-8")
    assert watcher._sidecar_quality(not_a_dict) is None


def test_sidecar_features_valid_and_invalid(tmp_path):
    # The agent writes the toggles nested under a "features" key (drop_into_inbox);
    # these fixtures mirror that real sidecar shape.
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"features": {"captions": True, "camera": False,
                                              "music": True, "animations": False}}), encoding="utf-8")
    assert watcher._sidecar_features(valid) == {
        "captions": True, "camera": False, "music": True, "animations": False}

    missing = tmp_path / "missing.json"
    assert watcher._sidecar_features(missing) == {}

    not_json = tmp_path / "not_json.json"
    not_json.write_text("{not json", encoding="utf-8")
    assert watcher._sidecar_features(not_json) == {}

    not_a_dict = tmp_path / "not_a_dict.json"
    not_a_dict.write_text(json.dumps(["captions"]), encoding="utf-8")
    assert watcher._sidecar_features(not_a_dict) == {}

    # features present but not an object -> nothing
    features_not_dict = tmp_path / "features_not_dict.json"
    features_not_dict.write_text(json.dumps({"features": "all"}), encoding="utf-8")
    assert watcher._sidecar_features(features_not_dict) == {}

    # no features key at all (e.g. sidecar with only anim_count) -> nothing
    no_features = tmp_path / "no_features.json"
    no_features.write_text(json.dumps({"anim_count": "few"}), encoding="utf-8")
    assert watcher._sidecar_features(no_features) == {}

    extra_keys = tmp_path / "extra_keys.json"
    extra_keys.write_text(json.dumps({"features": {"camera": False, "unknown": True}}), encoding="utf-8")
    assert watcher._sidecar_features(extra_keys) == {"camera": False}

    non_bool = tmp_path / "non_bool.json"
    non_bool.write_text(json.dumps({"features": {"sfx": 0, "camera": 1}}), encoding="utf-8")
    assert watcher._sidecar_features(non_bool) == {"sfx": False, "camera": True}


def test_sidecar_anim_count_valid_and_invalid(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"anim_count": "few"}), encoding="utf-8")
    assert watcher._sidecar_anim_count(valid) == "few"

    bad_value = tmp_path / "bad_value.json"
    bad_value.write_text(json.dumps({"anim_count": "lots"}), encoding="utf-8")
    assert watcher._sidecar_anim_count(bad_value) is None

    missing = tmp_path / "missing.json"
    assert watcher._sidecar_anim_count(missing) is None

    not_json = tmp_path / "not_json.json"
    not_json.write_text("{not json", encoding="utf-8")
    assert watcher._sidecar_anim_count(not_json) is None


def test_feature_flags_args_all_true_or_empty_and_none():
    assert watcher.feature_flags_args(
        {"captions": True, "camera": True, "animations": True}, None) == []
    assert watcher.feature_flags_args({}, None) == []
    assert watcher.feature_flags_args(None, None) == []


def test_feature_flags_args_exact_order():
    assert watcher.feature_flags_args(
        {"captions": False, "camera": False, "animations": False}, None) == [
        "--no-captions", "--no-camera", "--no-animations"]


def test_feature_flags_args_camera_and_animations_false():
    assert watcher.feature_flags_args({"camera": False, "animations": False}, None) == [
        "--no-camera", "--no-animations"]


def test_feature_flags_args_anim_count_only():
    assert watcher.feature_flags_args(None, "few") == ["--anim-count", "few"]
    assert watcher.feature_flags_args(None, "bogus") == []


def test_feature_flags_args_combined():
    assert watcher.feature_flags_args({"animations": False}, "max") == [
        "--no-animations", "--anim-count", "max"]


def test_feature_flags_args_music_and_sfx():
    # The two audio toggles: False emits --no-music / --no-sfx; True emits nothing.
    assert watcher.feature_flags_args({"music": False}, None) == ["--no-music"]
    assert watcher.feature_flags_args({"sfx": False}, None) == ["--no-sfx"]
    assert watcher.feature_flags_args({"music": True, "sfx": True}, None) == []


def test_feature_flags_args_all_video_toggles_false_exact_order():
    # Every video toggle off: the --no flags emit in _FEATURE_FLAGS order
    # (captions, camera, animations, music, sfx), then anim_count.
    assert watcher.feature_flags_args(
        {"captions": False, "camera": False,
         "animations": False, "music": False, "sfx": False}, "max") == [
        "--no-captions", "--no-camera", "--no-animations",
        "--no-music", "--no-sfx", "--anim-count", "max"]


def test_sidecar_features_includes_music_and_sfx(tmp_path):
    # music/sfx survive the nested-"features" read like the other toggles.
    p = tmp_path / "audio.json"
    p.write_text(json.dumps({"features": {"music": False, "sfx": True}}), encoding="utf-8")
    assert watcher._sidecar_features(p) == {"music": False, "sfx": True}


def test_task_args_video_with_music_sfx_features_appends_flags():
    task = _task("video")
    task["features"] = {"music": False, "sfx": False}
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue",
        "--no-music", "--no-sfx"]


def test_feature_flags_args_carousel_and_quotes():
    # The two deliverable toggles: False emits --no-carousel / --no-quotes.
    assert watcher.feature_flags_args({"carousel": False}, None) == ["--no-carousel"]
    assert watcher.feature_flags_args({"quotes": False}, None) == ["--no-quotes"]
    assert watcher.feature_flags_args({"carousel": True, "quotes": True}, None) == []


def test_feature_flags_args_all_seven_false_exact_order():
    # Every toggle off: seven --no flags in _FEATURE_FLAGS order, then anim_count.
    assert watcher.feature_flags_args(
        {"captions": False, "camera": False, "animations": False,
         "music": False, "sfx": False, "carousel": False, "quotes": False}, "max") == [
        "--no-captions", "--no-camera", "--no-animations",
        "--no-music", "--no-sfx", "--no-carousel", "--no-quotes",
        "--anim-count", "max"]


def test_task_args_video_with_carousel_quotes_features_appends_flags():
    task = _task("video")
    task["features"] = {"carousel": False, "quotes": False}
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue",
        "--no-carousel", "--no-quotes"]


def test_task_args_parent_run_id_does_not_add_fork():
    # --change-run forks the run internally (the change orchestrator), so
    # parent_run_id no longer emits a separate --fork flag.
    task = _task("video")
    task["parent_run_id"] = "7_short"
    assert watcher.task_args(task) == [
        "--change-run", "7_short", "--target", "video", "--notes=make it blue"]


def test_task_args_invalid_target_is_none():
    assert watcher.task_args(_task("bogus")) is None
    assert watcher.task_args(_task("video", run_id="")) is None


def _drop_mp4(inbox: Path, stem: str, content: bytes, sidecar: dict | None = None) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    mp4 = inbox / (stem + ".mp4")
    mp4.write_bytes(content)
    if sidecar is not None:
        (inbox / (stem + ".json")).write_text(json.dumps(sidecar), encoding="utf-8")
    return mp4


def test_mp4_enqueue_with_sidecar_quality_sets_args(tmp_path):
    inbox = tmp_path / "inbox"
    mp4 = _drop_mp4(inbox, "vidq", b"BYTES-1", sidecar={"anim_quality": "low"})
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidq"]
    assert rec["args"] == [str(mp4), "--short", "--anim-quality", "low"]


def test_mp4_enqueue_with_sidecar_features_and_anim_count_sets_args(tmp_path):
    inbox = tmp_path / "inbox"
    mp4 = _drop_mp4(inbox, "vidf", b"BYTES-F",
                     sidecar={"features": {"animations": False}, "anim_count": "few"})
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidf"]
    assert rec["args"] == [str(mp4), "--short", "--no-animations", "--anim-count", "few"]


def test_mp4_enqueue_with_sidecar_quality_and_features_sets_args(tmp_path):
    inbox = tmp_path / "inbox"
    mp4 = _drop_mp4(inbox, "vidqf", b"BYTES-QF",
                     sidecar={"anim_quality": "low", "features": {"camera": False, "sfx": False}})
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidqf"]
    assert rec["args"] == [str(mp4), "--short", "--anim-quality", "low",
                           "--no-camera", "--no-sfx"]


def test_mp4_enqueue_with_music_sfx_off_nested_in_features(tmp_path):
    # Guards the sidecar-nesting contract for the audio toggles: music/sfx off,
    # nested under "features" exactly as the agent writes it, must reach the CLI
    # as --no-music/--no-sfx (a top-level read would silently drop them).
    inbox = tmp_path / "inbox"
    mp4 = _drop_mp4(inbox, "vidaudio", b"BYTES-A",
                     sidecar={"features": {"music": False, "sfx": False}})
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidaudio"]
    assert rec["args"] == [str(mp4), "--short", "--no-music", "--no-sfx"]


def test_mp4_enqueue_with_carousel_quotes_off_nested_in_features(tmp_path):
    # Deliverable toggles off must reach the CLI as --no-carousel/--no-quotes
    # from the agent's nested "features" shape (same contract as the others).
    inbox = tmp_path / "inbox"
    mp4 = _drop_mp4(inbox, "viddeliv", b"BYTES-D",
                     sidecar={"features": {"carousel": False, "quotes": False}})
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/viddeliv"]
    assert rec["args"] == [str(mp4), "--short", "--no-carousel", "--no-quotes"]


def test_mp4_enqueue_without_sidecar_quality_has_no_args(tmp_path):
    inbox = tmp_path / "inbox"
    _drop_mp4(inbox, "vidplain", b"BYTES-2")
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidplain"]
    assert "args" not in rec


def test_mp4_replaced_with_sidecar_quality_sets_args(tmp_path):
    inbox = tmp_path / "inbox"
    mp4 = _drop_mp4(inbox, "vidr", b"ORIGINAL")
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    led["bionico/vidr"]["status"] = "done"
    # Genuinely different content, now with a quality sidecar.
    mp4 = _drop_mp4(inbox, "vidr", b"DIFFERENT BYTES", sidecar={"anim_quality": "max"})
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/vidr"]
    assert rec["status"] == "pending"
    assert rec["args"] == [str(mp4), "--short", "--anim-quality", "max"]


def test_scan_enqueues_task_file(tmp_path):
    inbox = tmp_path / "inbox"
    _write_task(inbox, "abc123.task.json", _task())
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/abc123"]
    assert rec["status"] == "pending"
    assert rec["args"] == ["--change-run", "7_short", "--target", "carousel", "--notes=make it blue"]
    assert rec["mp4"].endswith("abc123.task.json")


def test_same_task_done_is_skipped_but_new_nonce_requeues(tmp_path):
    inbox = tmp_path / "inbox"
    tf = _write_task(inbox, "abc123.task.json", _task(nonce=1))
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    led["bionico/abc123"]["status"] = "done"
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    assert led["bionico/abc123"]["status"] == "done"  # same bytes: skip
    _write_task(inbox, "abc123.task.json", _task(instructions="bigger title", nonce=2))
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/abc123"]
    assert rec["status"] == "pending"
    assert rec["args"] == ["--change-run", "7_short", "--target", "carousel", "--notes=bigger title"]


def test_failed_task_same_bytes_requeues(tmp_path):
    inbox = tmp_path / "inbox"
    tf = _write_task(inbox, "abc123.task.json", _task())
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    rec = led["bionico/abc123"]
    rec["status"] = "failed"
    rec["attempts"] = 2
    # a re-drop re-stamps mtime with the same bytes (offset avoids fs-resolution flakes)
    import os
    st = tf.stat()
    os.utime(tf, (st.st_atime, st.st_mtime + 100))
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    assert rec["status"] == "pending"
    assert rec["attempts"] == 0


def test_invalid_task_file_marks_failed(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "bad999.task.json").write_text("{not json", encoding="utf-8")
    led = {}
    watcher.scan_and_enqueue(led, inbox, tmp_path / "ledger.json")
    assert led["bionico/bad999"]["status"] == "failed"


def test_build_args_default_and_override(tmp_path):
    assert watcher.build_args({"mp4": "C:/x/v.mp4"}) == ["C:/x/v.mp4", "--short"]
    assert watcher.build_args({"mp4": "t.task.json", "args": ["--quotes-run", "7_short"]}) == [
        "--quotes-run", "7_short"]
