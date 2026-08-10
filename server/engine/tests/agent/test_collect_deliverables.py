"""V6 multi-format deliverable collection (agent.collect_deliverables).

A fake output folder holding EVERY V6 deliverable name proves: each file
classifies to its kind, a filename is collected exactly once (first pattern
wins), the classic V5-2 ordering survives untouched, a leftover entrega.zip is
never re-collected, and the change delta-filter keeps owning ONLY the classic
editable deliverables (new formats are non-editable in V6.0).
"""
from pathlib import Path

from bionico.agent import agent as ag
from bionico.agent.client import CONTENT_TYPES

# name -> expected kind, for every deliverable the V6 pipeline emits.
EXPECTED_KINDS = {
    # -- shipping deliverables (V5-2 behavior kept) --
    "final_7.mp4": "video",
    "quote_1.mp4": "quote",
    "quote_2.mp4": "quote",
    "quote_3.mp4": "quote",
    "carrusel_slide1.png": "slide",
    "carrusel_slide2.png": "slide",
    "caption.txt": "caption",
    "carousel.json": "meta",
    "quotes.json": "meta",
    # -- V6 extra videos --
    "clip_subtitulado.mp4": "video",
    "video_herotext.mp4": "video",
    "video_splitscreen.mp4": "video",
    "videocarrusel_voz_1.mp4": "video",
    "videocarrusel_voz_2.mp4": "video",
    "videocarrusel_musica.mp4": "video",
    "videocarrusel_teclado_1.mp4": "video",
    "videocarrusel_teclado_2.mp4": "video",
    "videocarrusel_whatsapp.mp4": "video",
    "videocarrusel_teclado_voz.mp4": "video",
    # -- V6 quote videos --
    "quote_foto_1.mp4": "quote",
    "quote_karaoke_1.mp4": "quote",
    # -- V6 carousel style variants --
    "carrusel_fotos_slide01.png": "slide",
    "carrusel_numeros_slide01.png": "slide",
    "carrusel_checklist_slide01.png": "slide",
    "carrusel_mitos_slide01.png": "slide",
    "carrusel_citas_slide01.png": "slide",
    "carrusel_editorial_slide01.png": "slide",
    "carrusel_stories_slide01.png": "slide",
    "carrusel_cuadrado_slide01.png": "slide",
    # -- V6 static images --
    "cita_1.png": "image",
    "cita_foto_1.png": "image",
    "citas_3en1.png": "image",
    "infografia_1.png": "image",
    "infografia_2.png": "image",
    "infografia_3.png": "image",
    "frase_solida.png": "image",
    "frase_foto.png": "image",
    "frase_plantilla.png": "image",
    # -- V6 texts --
    "primer_comentario.txt": "caption",
    "youtube_short.txt": "caption",
    "post_x.txt": "caption",
    "hilo_x.txt": "caption",
    "articulo_x.txt": "caption",
    "linkedin.txt": "caption",
    "newsletter.txt": "caption",
    "blog.txt": "caption",
    "ganchos.txt": "caption",
    "subtitulos.srt": "caption",
    # -- V6 pdf / manifest (no pack_*.zip: the Worker zips groups on demand) --
    "carrusel_linkedin.pdf": "pdf",
    "formats_manifest.json": "meta",
}


def _make_run_folder(tmp_path: Path, names=None) -> Path:
    folder = tmp_path / "run_7"
    folder.mkdir()
    for name in (EXPECTED_KINDS if names is None else names):
        (folder / name).write_bytes(b"X-" + name.encode("ascii"))
    return folder


def test_collects_every_v6_deliverable_with_its_kind(tmp_path):
    folder = _make_run_folder(tmp_path)
    # A leftover master zip from a previous publish must never be re-collected.
    (folder / "entrega.zip").write_bytes(b"OLDZIP")
    files = ag.collect_deliverables(folder)
    collected = {p.name: kind for p, kind in files}
    assert collected == EXPECTED_KINDS
    assert "entrega.zip" not in collected


def test_each_filename_collected_exactly_once(tmp_path):
    # caption.txt also matches *.txt and quote_foto_1.mp4 also matches
    # quote_*.mp4: dedupe keeps the FIRST match only.
    folder = _make_run_folder(tmp_path)
    names = [p.name for p, _ in ag.collect_deliverables(folder)]
    assert len(names) == len(set(names))
    assert names.count("caption.txt") == 1
    assert names.count("quote_foto_1.mp4") == 1


def test_video_first_and_final_is_the_first_video(tmp_path):
    # The worker's complete() takes the FIRST kind=video entry as output_key:
    # it must stay final_*.mp4, never a V6 extra video.
    folder = _make_run_folder(tmp_path)
    files = ag.collect_deliverables(folder)
    assert files[0][0].name == "final_7.mp4"
    first_video = next(p.name for p, kind in files if kind == "video")
    assert first_video == "final_7.mp4"


def test_classic_only_folder_keeps_v5_order(tmp_path):
    # Back-compat: a pre-V6 run folder collects exactly like V5-2 did.
    classic = ["final_7.mp4", "quote_1.mp4", "quote_2.mp4", "quote_3.mp4",
               "carrusel_slide1.png", "carrusel_slide2.png", "caption.txt",
               "carousel.json", "quotes.json"]
    folder = _make_run_folder(tmp_path, classic)
    files = ag.collect_deliverables(folder)
    assert [(p.name, kind) for p, kind in files] == [
        ("final_7.mp4", "video"),
        ("quote_1.mp4", "quote"), ("quote_2.mp4", "quote"), ("quote_3.mp4", "quote"),
        ("carrusel_slide1.png", "slide"), ("carrusel_slide2.png", "slide"),
        ("caption.txt", "caption"),
        ("carousel.json", "meta"), ("quotes.json", "meta"),
    ]


def test_owns_video_final_only():
    # Video changes own final_*.mp4 ONLY: the V6 extra formats are
    # non-editable in V6.0 (the forked run carries their copies unchanged).
    assert ag._owns("video", "final_7.mp4")
    assert not ag._owns("video", "clip_subtitulado.mp4")
    assert not ag._owns("video", "video_splitscreen.mp4")
    assert not ag._owns("video", "video_herotext.mp4")
    assert not ag._owns("video", "videocarrusel_voz_1.mp4")
    assert not ag._owns("video", "subtitulos.srt")


def test_owns_quotes_excludes_v6_quote_variants():
    assert ag._owns("quotes", "quote_1.mp4")
    assert ag._owns("quotes", "quotes.json")
    assert ag._owns("quotes", "caption.txt")
    assert not ag._owns("quotes", "quote_foto_1.mp4")
    assert not ag._owns("quotes", "quote_karaoke_2.mp4")


def test_owns_carousel_excludes_v6_carousel_variants():
    assert ag._owns("carousel", "carrusel_slide3.png")
    assert ag._owns("carousel", "carousel.json")
    assert ag._owns("carousel", "caption.txt")
    assert not ag._owns("carousel", "carrusel_fotos_slide01.png")
    assert not ag._owns("carousel", "carrusel_linkedin.pdf")
    assert not ag._owns("carousel", "pack_carruseles.zip")


def test_delta_filter_video_change_publishes_only_final(tmp_path):
    folder = _make_run_folder(tmp_path)
    files = ag.collect_deliverables(folder)
    delta = ag._delta_filter(files, {"video"})
    assert [(p.name, kind) for p, kind in delta] == [("final_7.mp4", "video")]


def test_delta_filter_quotes_change_publishes_only_classic_quote_files(tmp_path):
    folder = _make_run_folder(tmp_path)
    files = ag.collect_deliverables(folder)
    delta = ag._delta_filter(files, {"quotes"})
    names = sorted(p.name for p, _ in delta)
    assert names == ["caption.txt", "quote_1.mp4", "quote_2.mp4", "quote_3.mp4",
                     "quotes.json"]


def test_content_types_include_srt_and_pdf():
    assert CONTENT_TYPES[".srt"] == "text/plain"
    assert CONTENT_TYPES[".pdf"] == "application/pdf"
