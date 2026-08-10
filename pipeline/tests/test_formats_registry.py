from contenido_bionico.short.formats import registry


def test_all_specs_well_formed():
    assert registry.FORMATS, "registry must not be empty"
    seen_outputs = set()
    for key, spec in registry.FORMATS.items():
        assert spec.key == key
        assert spec.category in registry.CATEGORIES
        assert ":" in spec.producer and spec.producer.startswith("contenido_bionico.short.formats.")
        assert spec.outputs, key
        for glob in spec.outputs:
            assert glob not in seen_outputs, f"duplicate output {glob}"
            seen_outputs.add(glob)
        assert spec.kind in ("video", "quote", "slide", "image", "text", "caption", "pdf", "meta")


def test_every_producer_resolves():
    for spec in registry.FORMATS.values():
        assert callable(registry.resolve_producer(spec))


def test_expected_keys_present():
    expected = {
        "clip_subtitulado", "video_herotext", "video_splitscreen",
        "videocarrusel_voz_1", "videocarrusel_voz_2", "videocarrusel_musica",
        "videocarrusel_teclado", "videocarrusel_whatsapp", "videocarrusel_teclado_voz",
        "carrusel_fotos", "carrusel_numeros", "carrusel_checklist", "carrusel_mitos",
        "carrusel_citas", "carrusel_editorial", "carrusel_stories", "carrusel_cuadrado",
        "carrusel_linkedin_pdf",
        "quote_foto", "quote_karaoke", "quote_tecleado",
        "cita_estatica", "cita_foto", "citas_3en1", "infografia",
        "frase_solida", "frase_foto", "frase_plantilla",
        "poster_1", "poster_1_foto", "poster_2", "poster_2_foto",
        "poster_3", "poster_3_foto", "poster_4", "poster_4_foto",
        "poster_video_1", "poster_video_2", "poster_video_3", "poster_video_4",
        "poster_video_1_foto", "poster_video_2_foto", "poster_video_3_foto", "poster_video_4_foto",
        "textos_social", "textos_longform", "subtitulos_srt",
    }
    assert expected == set(registry.FORMATS)


def test_toggle_for_category_covers_all_categories():
    for category in registry.CATEGORIES:
        assert category in registry.TOGGLE_FOR_CATEGORY


def test_formats_for_category():
    for spec in registry.formats_for_category("textos"):
        assert spec.category == "textos"
    assert {s.key for s in registry.formats_for_category("textos")} == {
        "textos_social", "textos_longform", "subtitulos_srt",
    }
