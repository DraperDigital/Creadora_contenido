"""Output-format registry: WHAT each deliverable is, WHICH atoms it needs, WHO renders it.
Plumbing only — never encodes creative/visual decisions (those live in the comps/agents)."""
from __future__ import annotations
import importlib
from dataclasses import dataclass
from typing import Callable

CATEGORIES = ("videos", "video_carruseles", "carruseles", "citas", "imagenes", "posters", "textos")

# requires vocabulary (checked by runner against disk): transcript, source, voice,
# captions_webm, captions_props, carousel_plan, quote_plan, style_tokens, broll
@dataclass(frozen=True)
class FormatSpec:
    key: str
    category: str
    label: str                      # Spanish, user-facing (dashboard)
    producer: str                   # "contenido_bionico.short.formats.<mod>:<func>"
    requires: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()   # output-dir globs
    kind: str = "image"
    default_on: bool = True

def _f(**kw) -> FormatSpec: return FormatSpec(**kw)
_P = "contenido_bionico.short.formats."

FORMATS: dict[str, FormatSpec] = {s.key: s for s in [
    # -- videos --
    _f(key="clip_subtitulado", category="videos", label="Clip subtitulado",
       producer=_P+"video_treatments:produce_clip_subtitulado",
       requires=("source", "captions_webm", "voice"), outputs=("clip_subtitulado.mp4",), kind="video"),
    _f(key="video_herotext", category="videos", label="Video con texto protagonista",
       producer=_P+"video_treatments:produce_video_herotext",
       requires=("source", "transcript", "voice", "style_tokens"), outputs=("video_herotext.mp4",), kind="video"),
    _f(key="video_splitscreen", category="videos", label="Video pantalla dividida",
       producer=_P+"video_treatments:produce_video_splitscreen",
       requires=("source", "transcript", "voice", "style_tokens"), outputs=("video_splitscreen.mp4",), kind="video"),
    # -- video_carruseles --
    _f(key="videocarrusel_voz_1", category="video_carruseles", label="Video-carrusel con tu voz (1)",
       producer=_P+"video_carousels:produce_voz_1",
       requires=("carousel_plan", "voice", "transcript"), outputs=("videocarrusel_voz_1.mp4",), kind="video"),
    _f(key="videocarrusel_voz_2", category="video_carruseles", label="Video-carrusel con tu voz (2)",
       producer=_P+"video_carousels:produce_voz_2",
       requires=("carousel_plan", "voice", "transcript"), outputs=("videocarrusel_voz_2.mp4",), kind="video"),
    _f(key="videocarrusel_musica", category="video_carruseles", label="Video-carrusel con música",
       producer=_P+"video_carousels:produce_musica",
       requires=("carousel_plan",), outputs=("videocarrusel_musica.mp4",), kind="video"),
    _f(key="videocarrusel_teclado", category="video_carruseles", label="Clips tecleados (uno por idea)",
       producer=_P+"video_carousels:produce_teclado",
       requires=("carousel_plan",), outputs=("videocarrusel_teclado_*.mp4",), kind="video"),
    _f(key="videocarrusel_whatsapp", category="video_carruseles", label="Video-carrusel WhatsApp",
       producer=_P+"video_carousels:produce_whatsapp",
       requires=("carousel_plan",), outputs=("videocarrusel_whatsapp.mp4",), kind="video"),
    _f(key="videocarrusel_teclado_voz", category="video_carruseles", label="Video-carrusel tecleado con voz",
       producer=_P+"video_carousels:produce_teclado_voz",
       requires=("carousel_plan", "voice", "transcript"), outputs=("videocarrusel_teclado_voz.mp4",), kind="video"),
    # -- carruseles --
    _f(key="carrusel_fotos", category="carruseles", label="Carrusel sobre tus fotos",
       producer=_P+"carousels:produce_fotos",
       requires=("carousel_plan", "broll"), outputs=("carrusel_fotos_slide*.png",), kind="slide"),
    _f(key="carrusel_numeros", category="carruseles", label="Carrusel de números",
       producer=_P+"carousels:produce_numeros",
       requires=("carousel_plan",), outputs=("carrusel_numeros_slide*.png",), kind="slide"),
    _f(key="carrusel_checklist", category="carruseles", label="Carrusel checklist",
       producer=_P+"carousels:produce_checklist",
       requires=("carousel_plan",), outputs=("carrusel_checklist_slide*.png",), kind="slide"),
    _f(key="carrusel_mitos", category="carruseles", label="Carrusel mito vs realidad",
       producer=_P+"carousels:produce_mitos",
       requires=("carousel_plan", "transcript"), outputs=("carrusel_mitos_slide*.png",), kind="slide"),
    _f(key="carrusel_citas", category="carruseles", label="Carrusel de frases",
       producer=_P+"carousels:produce_citas",
       requires=("quote_plan",), outputs=("carrusel_citas_slide*.png",), kind="slide"),
    _f(key="carrusel_editorial", category="carruseles", label="Carrusel editorial",
       producer=_P+"carousels:produce_editorial",
       requires=("carousel_plan",), outputs=("carrusel_editorial_slide*.png",), kind="slide"),
    _f(key="carrusel_stories", category="carruseles", label="Carrusel formato Stories",
       producer=_P+"carousels:produce_stories",
       requires=("carousel_plan",), outputs=("carrusel_stories_slide*.png",), kind="slide"),
    _f(key="carrusel_cuadrado", category="carruseles", label="Carrusel cuadrado",
       producer=_P+"carousels:produce_cuadrado",
       requires=("carousel_plan",), outputs=("carrusel_cuadrado_slide*.png",), kind="slide"),
    _f(key="carrusel_linkedin_pdf", category="carruseles", label="Carrusel PDF (LinkedIn)",
       producer=_P+"carousels:produce_linkedin_pdf",
       requires=("carousel_plan",), outputs=("carrusel_linkedin.pdf",), kind="pdf"),
    # -- citas --
    _f(key="quote_foto", category="citas", label="Frase en video sobre tu foto",
       producer=_P+"quote_videos:produce_quote_foto",
       requires=("quote_plan", "broll"), outputs=("quote_foto_*.mp4",), kind="quote"),
    _f(key="quote_karaoke", category="citas", label="Frase animada con tu voz",
       producer=_P+"quote_videos:produce_quote_karaoke",
       requires=("quote_plan", "voice", "transcript", "style_tokens"), outputs=("quote_karaoke_*.mp4",), kind="quote"),
    _f(key="quote_tecleado", category="citas", label="Frase tecleada (video)",
       producer=_P+"quote_videos:produce_quote_tecleado",
       requires=("quote_plan",), outputs=("quote_tecleado_*.mp4",), kind="quote"),
    # -- imagenes --
    _f(key="cita_estatica", category="imagenes", label="Frases en imagen",
       producer=_P+"images:produce_cita_estatica",
       requires=("quote_plan",), outputs=("cita_*.png",), kind="image"),
    _f(key="cita_foto", category="imagenes", label="Frase sobre tu foto",
       producer=_P+"images:produce_cita_foto",
       requires=("quote_plan", "broll"), outputs=("cita_foto_*.png",), kind="image"),
    _f(key="citas_3en1", category="imagenes", label="Tres frases en una imagen",
       producer=_P+"images:produce_citas_3en1",
       requires=("quote_plan", "style_tokens"), outputs=("citas_3en1.png",), kind="image"),
    _f(key="infografia", category="imagenes", label="Infografía resumen (3 versiones)",
       producer=_P+"images:produce_infografia",
       requires=("carousel_plan", "style_tokens"), outputs=("infografia_*.png",), kind="image"),
    _f(key="frase_solida", category="imagenes", label="Frase sobre color sólido",
       producer=_P+"images:produce_frase_solida",
       requires=("quote_plan", "style_tokens"), outputs=("frase_solida.png",), kind="image"),
    _f(key="frase_foto", category="imagenes", label="Frase hero sobre tu foto",
       producer=_P+"images:produce_frase_foto",
       requires=("quote_plan", "broll"), outputs=("frase_foto.png",), kind="image"),
    _f(key="frase_plantilla", category="imagenes", label="Frase sobre plantilla de marca",
       producer=_P+"images:produce_frase_plantilla",
       requires=("quote_plan", "style_tokens"), outputs=("frase_plantilla.png",), kind="image"),
    # -- posters (giant type behind subject; 4 layouts x solid/foto background) --
    # Layouts are element distributions only — no theme/genre; all share one
    # neutral transcript distillation. `_foto` variants use the real photo plate.
    _f(key="poster_1", category="posters", label="Póster 1",
       producer=_P+"posters:produce_1",
       requires=("transcript",), outputs=("poster_1.png",), kind="image"),
    _f(key="poster_1_foto", category="posters", label="Póster 1 (sobre foto)",
       producer=_P+"posters:produce_1_foto",
       requires=("transcript", "broll"), outputs=("poster_1_foto.png",), kind="image"),
    _f(key="poster_2", category="posters", label="Póster 2",
       producer=_P+"posters:produce_2",
       requires=("transcript",), outputs=("poster_2.png",), kind="image"),
    _f(key="poster_2_foto", category="posters", label="Póster 2 (sobre foto)",
       producer=_P+"posters:produce_2_foto",
       requires=("transcript", "broll"), outputs=("poster_2_foto.png",), kind="image"),
    _f(key="poster_3", category="posters", label="Póster 3",
       producer=_P+"posters:produce_3",
       requires=("transcript",), outputs=("poster_3.png",), kind="image"),
    _f(key="poster_3_foto", category="posters", label="Póster 3 (sobre foto)",
       producer=_P+"posters:produce_3_foto",
       requires=("transcript", "broll"), outputs=("poster_3_foto.png",), kind="image"),
    _f(key="poster_4", category="posters", label="Póster 4",
       producer=_P+"posters:produce_4",
       requires=("transcript",), outputs=("poster_4.png",), kind="image"),
    _f(key="poster_4_foto", category="posters", label="Póster 4 (sobre foto)",
       producer=_P+"posters:produce_4_foto",
       requires=("transcript", "broll"), outputs=("poster_4_foto.png",), kind="image"),
    # -- posters animados: one SOLID mp4 (distinct foreground) + one FOTO mp4
    #    (distinct cutout pair) per layout; text animates in, music + SFX --
    _f(key="poster_video_1", category="posters", label="Póster 1 animado",
       producer=_P+"posters_video:produce_1",
       requires=("transcript",), outputs=("poster_video_1.mp4",), kind="video"),
    _f(key="poster_video_2", category="posters", label="Póster 2 animado",
       producer=_P+"posters_video:produce_2",
       requires=("transcript",), outputs=("poster_video_2.mp4",), kind="video"),
    _f(key="poster_video_3", category="posters", label="Póster 3 animado",
       producer=_P+"posters_video:produce_3",
       requires=("transcript",), outputs=("poster_video_3.mp4",), kind="video"),
    _f(key="poster_video_4", category="posters", label="Póster 4 animado",
       producer=_P+"posters_video:produce_4",
       requires=("transcript",), outputs=("poster_video_4.mp4",), kind="video"),
    _f(key="poster_video_1_foto", category="posters", label="Póster 1 animado (foto)",
       producer=_P+"posters_video:produce_1_foto",
       requires=("transcript", "broll"), outputs=("poster_video_1_foto.mp4",), kind="video"),
    _f(key="poster_video_2_foto", category="posters", label="Póster 2 animado (foto)",
       producer=_P+"posters_video:produce_2_foto",
       requires=("transcript", "broll"), outputs=("poster_video_2_foto.mp4",), kind="video"),
    _f(key="poster_video_3_foto", category="posters", label="Póster 3 animado (foto)",
       producer=_P+"posters_video:produce_3_foto",
       requires=("transcript", "broll"), outputs=("poster_video_3_foto.mp4",), kind="video"),
    _f(key="poster_video_4_foto", category="posters", label="Póster 4 animado (foto)",
       producer=_P+"posters_video:produce_4_foto",
       requires=("transcript", "broll"), outputs=("poster_video_4_foto.mp4",), kind="video"),
    # -- textos --
    _f(key="textos_social", category="textos", label="Textos para publicar",
       producer=_P+"texts:produce_social",
       requires=("transcript",),
       outputs=("caption.txt", "primer_comentario.txt", "youtube_short.txt", "post_x.txt", "ganchos.txt"),
       kind="caption"),
    _f(key="textos_longform", category="textos", label="Textos largos",
       producer=_P+"texts:produce_longform",
       requires=("transcript",),
       outputs=("hilo_x.txt", "articulo_x.txt", "linkedin.txt", "newsletter.txt", "blog.txt"),
       kind="caption"),
    _f(key="subtitulos_srt", category="textos", label="Subtítulos SRT",
       producer=_P+"formats_srt:produce_srt",
       requires=("captions_props",), outputs=("subtitulos.srt",), kind="caption"),
]}

TOGGLE_FOR_CATEGORY = {"videos": "videos_extra", "video_carruseles": "video_carruseles",
                       "carruseles": "carruseles_extra", "citas": "quotes",
                       "imagenes": "imagenes", "posters": "posters", "textos": "textos"}

def formats_for_category(category: str) -> list[FormatSpec]:
    return [s for s in FORMATS.values() if s.category == category]

def resolve_producer(spec: FormatSpec) -> Callable:
    mod_name, func_name = spec.producer.split(":", 1)
    return getattr(importlib.import_module(mod_name), func_name)
