"""Tests for the b-roll photo library (WS5 real implementation).

Covers: `list_photos` filtering (suffix, size, cutout-sidecar exclusion),
`select_photos` unbiased per-(run, purpose) shuffle (distinct slots, varied per
format and per run, reproducible), and `person_cutout` sidecar caching with a
monkeypatched rembg loader.
"""
from types import SimpleNamespace

from contenido_bionico.short.formats import broll


def _photo(path, size=20 * 1024):
    path.write_bytes(b"\x89PNG" + b"0" * size)
    return path


def _ctx(video_id):
    return SimpleNamespace(video_id=video_id)


# --- list_photos ---------------------------------------------------------------


def test_list_photos_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path / "nope")
    assert broll.list_photos() == []


def test_list_photos_sorted_and_suffix_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    _photo(tmp_path / "b.jpg")
    _photo(tmp_path / "a.png")
    _photo(tmp_path / "c.webp")
    (tmp_path / "notes.txt").write_bytes(b"x" * (20 * 1024))
    (tmp_path / "clip.mp4").write_bytes(b"x" * (20 * 1024))
    names = [p.name for p in broll.list_photos()]
    assert names == ["a.png", "b.jpg", "c.webp"]


def test_list_photos_excludes_small_files(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    _photo(tmp_path / "big.jpg", size=20 * 1024)
    (tmp_path / "tiny.jpg").write_bytes(b"x" * 1024)  # < 10KB
    names = [p.name for p in broll.list_photos()]
    assert names == ["big.jpg"]


def test_list_photos_excludes_cutout_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    _photo(tmp_path / "a.jpg")
    # A cutout sidecar has a .png suffix but must never be treated as a photo.
    _photo(tmp_path / "a.jpg.cutout.png")
    names = [p.name for p in broll.list_photos()]
    assert names == ["a.jpg"]


# --- select_photos -------------------------------------------------------------


def _stub_photos(monkeypatch, n):
    photos = [SimpleNamespace(name=f"p{i}") for i in range(n)]
    monkeypatch.setattr(broll, "list_photos", lambda: list(photos))
    return photos


def test_select_photos_returns_all_when_fewer_than_n(tmp_path, monkeypatch):
    photos = _stub_photos(monkeypatch, 3)
    got = broll.select_photos(_ctx("5_short"), 10, "fondo")
    assert {p.name for p in got} == {p.name for p in photos} and len(got) == 3  # all (shuffled)


def test_select_photos_zero_returns_empty(monkeypatch):
    _stub_photos(monkeypatch, 5)
    assert broll.select_photos(_ctx("5_short"), 0, "fondo") == []


def test_select_photos_deterministic_for_same_run_and_purpose(monkeypatch):
    _stub_photos(monkeypatch, 20)
    a = broll.select_photos(_ctx("5_short"), 3, "fondo")
    b = broll.select_photos(_ctx("5_short"), 3, "fondo")
    assert a == b and len(a) == 3            # reproducible per (run, purpose)


def test_select_photos_distinct_and_varies_per_purpose(monkeypatch):
    _stub_photos(monkeypatch, 20)
    carrusel = broll.select_photos(_ctx("5_short"), 8, "carrusel")
    assert len({p.name for p in carrusel}) == 8          # every slot distinct
    cita = broll.select_photos(_ctx("5_short"), 8, "cita")
    assert carrusel != cita                              # different purpose -> different photos


def test_select_photos_varies_across_runs(monkeypatch):
    _stub_photos(monkeypatch, 20)
    a = broll.select_photos(_ctx("5_short"), 3, "fondo")
    b = broll.select_photos(_ctx("6_short"), 3, "fondo")
    assert a != b


# --- person_cutout -------------------------------------------------------------


def test_person_cutout_none_when_rembg_unavailable(tmp_path, monkeypatch):
    photo = _photo(tmp_path / "a.jpg")
    monkeypatch.setattr(broll, "_load_rembg_remove", lambda: None)
    assert broll.person_cutout(photo, tmp_path / "out.png") is None


def test_person_cutout_none_when_rembg_raises(tmp_path, monkeypatch):
    photo = _photo(tmp_path / "a.jpg")

    def boom(_data):
        raise RuntimeError("segmentation blew up")

    monkeypatch.setattr(broll, "_load_rembg_remove", lambda: boom)
    assert broll.person_cutout(photo, tmp_path / "out.png") is None


def test_person_cutout_writes_output_and_sidecar_cache(tmp_path, monkeypatch):
    photo = _photo(tmp_path / "a.jpg")
    calls = {"n": 0}

    def fake_remove(data):
        calls["n"] += 1
        return b"CUTOUTPNGDATA"

    monkeypatch.setattr(broll, "_load_rembg_remove", lambda: fake_remove)
    out = tmp_path / "staged" / "out.png"
    result = broll.person_cutout(photo, out)
    assert result == out
    assert out.exists()
    sidecar = photo.parent / (photo.name + ".cutout.png")
    assert sidecar.exists()
    assert sidecar.read_bytes() == b"CUTOUTPNGDATA"
    assert calls["n"] == 1


def test_person_cutout_reuses_fresh_cache(tmp_path, monkeypatch):
    photo = _photo(tmp_path / "a.jpg")
    calls = {"n": 0}

    def fake_remove(data):
        calls["n"] += 1
        return b"DATA"

    monkeypatch.setattr(broll, "_load_rembg_remove", lambda: fake_remove)
    broll.person_cutout(photo, tmp_path / "o1.png")
    broll.person_cutout(photo, tmp_path / "o2.png")
    # Second call reused the sidecar cache: rembg ran only once.
    assert calls["n"] == 1
    assert (tmp_path / "o2.png").read_bytes() == b"DATA"


def test_person_cutout_recomputes_when_photo_newer(tmp_path, monkeypatch):
    import os

    photo = _photo(tmp_path / "a.jpg")
    calls = {"n": 0}

    def fake_remove(data):
        calls["n"] += 1
        return b"DATA"

    monkeypatch.setattr(broll, "_load_rembg_remove", lambda: fake_remove)
    broll.person_cutout(photo, tmp_path / "o1.png")
    assert calls["n"] == 1
    # Photo edited after the cutout was cached -> stale cache -> recompute.
    future = photo.stat().st_mtime + 1000
    os.utime(photo, (future, future))
    broll.person_cutout(photo, tmp_path / "o2.png")
    assert calls["n"] == 2


# --- cutout pairs (text-behind-person) -----------------------------------------


def _png(path, size, *, alpha):
    """Write a real, >10KB PNG at `path`; RGBA (with a transparent pixel) when
    alpha. Noise keeps it above `_MIN_PHOTO_BYTES` (a solid fill would compress
    below the floor and be filtered out)."""
    import os

    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGBA" if alpha else "RGB"
    channels = 4 if alpha else 3
    w, h = 256, 256  # noisy 256x256 -> PNG well over the 10KB floor
    im = Image.frombytes(mode, (w, h), os.urandom(w * h * channels))
    if alpha:
        im.putpixel((0, 0), (0, 0, 0, 0))  # a genuinely transparent pixel
    im.save(path)
    return path


def test_list_cutout_pairs_matches_front_to_back(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    _png(cut / "standing back.png", (60, 100), alpha=False)   # opaque plate
    _png(cut / "standing front.png", (60, 100), alpha=True)   # transparent cutout
    pairs = broll.list_cutout_pairs()
    assert len(pairs) == 1
    bg, fg = pairs[0]
    assert bg.name == "standing back.png"
    assert fg.name == "standing front.png"


def test_list_cutout_pairs_lone_cutout_has_no_background(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    _png(cut / "solo front.png", (60, 100), alpha=True)
    pairs = broll.list_cutout_pairs()
    assert pairs == [(None, cut / "solo front.png")] or (
        len(pairs) == 1 and pairs[0][0] is None and pairs[0][1].name == "solo front.png"
    )


def test_list_cutout_pairs_missing_dir_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)  # no cutouts/ subdir
    assert broll.list_cutout_pairs() == []


def test_select_cutout_pair_reproducible_and_varies(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    for n in (1, 2, 3, 4, 5):
        _png(cut / f"background_{n}.png", (60, 100), alpha=False)
        _png(cut / f"foreground_{n}.png", (60, 100), alpha=True)
    # Reproducible per run (an edit re-render lands on the same pair).
    first = broll.select_cutout_pair(_ctx("6_short"))
    assert first is not None and broll.select_cutout_pair(_ctx("6_short"))[1] == first[1]
    # Unbiased shuffle: across many runs the pick is NOT constant (no fixed bias).
    picks = {broll.select_cutout_pair(_ctx(f"{r}_short"))[1].name for r in range(1, 20)}
    assert len(picks) > 1


def test_select_cutout_pair_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path / "nope")
    assert broll.select_cutout_pair(_ctx("6_short")) is None


def test_select_cutout_pair_for_index_varies_pair(tmp_path, monkeypatch):
    """Sibling foto posters (index 0..3) each get a DIFFERENT complete pair, and
    each (run, index) is stable so a poster's png and mp4 use the same combo."""
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    for n in (1, 2, 3, 4):
        _png(cut / f"background_{n}.png", (60, 100), alpha=False)
        _png(cut / f"foreground_{n}.png", (60, 100), alpha=True)
    ctx = _ctx("6_short")
    picks = [broll.select_cutout_pair_for(ctx, i) for i in range(4)]
    assert all(p is not None and p[0] is not None for p in picks)
    assert len({p[1].name for p in picks}) == 4          # four distinct foregrounds
    assert broll.select_cutout_pair_for(ctx, 2)[1] == picks[2][1]  # stable per (run, index)


# --- subject cutouts (posters) -------------------------------------------------


def test_list_subject_cutouts_returns_transparent_fgs(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    _png(cut / "a back.png", (60, 100), alpha=False)
    _png(cut / "a front.png", (60, 100), alpha=True)
    _png(cut / "b front.png", (60, 100), alpha=True)
    names = sorted(p.name for p in broll.list_subject_cutouts())
    assert names == ["a front.png", "b front.png"]  # only the alpha cutouts


def test_select_subject_cutout_uses_cutouts_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    _png(cut / "solo front.png", (60, 100), alpha=True)
    got = broll.select_subject_cutout(_ctx("5_short"))
    assert got is not None and got.name == "solo front.png"


def test_select_subject_cutout_none_without_cutout(tmp_path, monkeypatch):
    # Cutouts are NEVER generated from library photos: no cutout -> None (the
    # poster renders type-only), even if b-roll photos exist.
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    _photo(tmp_path / "person.jpg")
    assert broll.select_subject_cutout(_ctx("5_short")) is None


def test_select_subject_cutout_none_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path / "empty")
    assert broll.select_subject_cutout(_ctx("5_short")) is None


def test_select_subject_cutout_for_index_varies_cutout(tmp_path, monkeypatch):
    """Sibling SOLID posters (index 0..3) each composite a DIFFERENT foreground."""
    monkeypatch.setattr(broll, "BROLL_DIR", tmp_path)
    cut = tmp_path / broll.CUTOUTS_DIRNAME
    for n in (1, 2, 3, 4):
        _png(cut / f"foreground_{n}.png", (60, 100), alpha=True)
    ctx = _ctx("6_short")
    picks = [broll.select_subject_cutout_for(ctx, i) for i in range(4)]
    assert all(p is not None for p in picks)
    assert len({p.name for p in picks}) == 4              # four distinct foregrounds
    assert broll.select_subject_cutout_for(ctx, 1) == picks[1]  # stable per (run, index)
