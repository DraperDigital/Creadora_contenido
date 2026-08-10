import zipfile

from contenido_bionico.short.formats.packs import build_category_packs


def test_pack_per_nonempty_category(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"a")
    (tmp_path / "b.png").write_bytes(b"b")
    manifest = {"categories": {
        "videos": [{"file": "a.mp4"}],
        "imagenes": [{"file": "b.png"}],
        "textos": [],
    }}
    packs = build_category_packs(tmp_path, manifest)
    assert {p.name for p in packs} == {"pack_videos.zip", "pack_imagenes.zip"}
    assert not (tmp_path / "pack_textos.zip").exists()
    with zipfile.ZipFile(tmp_path / "pack_videos.zip") as zf:
        assert zf.namelist() == ["a.mp4"]


def test_pack_excludes_manifest_and_other_packs(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"a")
    (tmp_path / "formats_manifest.json").write_bytes(b"{}")
    (tmp_path / "pack_other.zip").write_bytes(b"z")
    manifest = {"categories": {"videos": [
        {"file": "a.mp4"},
        {"file": "formats_manifest.json"},
        {"file": "pack_other.zip"},
    ]}}
    build_category_packs(tmp_path, manifest)
    with zipfile.ZipFile(tmp_path / "pack_videos.zip") as zf:
        assert zf.namelist() == ["a.mp4"]


def test_pack_skips_when_all_files_missing(tmp_path):
    manifest = {"categories": {"videos": [{"file": "ghost.mp4"}]}}
    assert build_category_packs(tmp_path, manifest) == []
    assert not (tmp_path / "pack_videos.zip").exists()


def test_pack_stored_compression(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"a" * 100)
    manifest = {"categories": {"videos": [{"file": "a.mp4"}]}}
    build_category_packs(tmp_path, manifest)
    with zipfile.ZipFile(tmp_path / "pack_videos.zip") as zf:
        assert zf.getinfo("a.mp4").compress_type == zipfile.ZIP_STORED
