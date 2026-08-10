"""WS1 text-deliverable producers.

The agent call is monkeypatched to return canned JSON on stdout, so these tests
exercise the real message-building, tolerant JSON parse, and EXACT file-writing
logic without touching the `claude` CLI.
"""
import json
from types import SimpleNamespace

import pytest

from contenido_bionico.short.formats import texts
from contenido_bionico.short.formats.runner import FormatContext


# --- canned agent outputs -------------------------------------------------

SOCIAL_JSON = {
    "caption": "Gancho que frena el scroll.\nCuerpo con la idea.\nCierre con CTA.",
    "hashtags": ["#Ventas", "#Negocios", "#Emprender"],
    "primer_comentario": "Cuentame en los comentarios como lo aplicas.",
    "youtube_title": "Como vender mas sin mas seguidores",
    "youtube_description": "Aprende el sistema en 60 segundos.\nSigueme para mas.",
    "post_x": "El alcance no paga: si nadie te compra, el problema es la conversion.",
    "ganchos": [
        "Gancho alternativo uno.",
        "Gancho alternativo dos.",
        "Gancho alternativo tres.",
    ],
}

LONGFORM_JSON = {
    "hilo_x": ["1/3 Gancho del hilo.", "2/3 Punto intermedio.", "3/3 Cierre del hilo."],
    "articulo_x": "Titulo del articulo\n\nParrafo del articulo.",
    "linkedin": "Primera linea.\n\nDesarrollo.\n\nCierre.",
    "newsletter": {
        "asuntos": ["Asunto A", "Asunto B", "Asunto C"],
        "cuerpo": "Cuerpo de la newsletter con la idea del video.",
    },
    "blog": "Titulo\n\nIntro.\n\nPRIMERA SECCION\n\nDesarrollo.",
}

# Deliberately markdown-laden payloads: the plain-text defense layer must
# scrub every one of these before the files are written.

MARKDOWN_SOCIAL_JSON = {
    "caption": "## Gancho fuerte\n**Cuerpo** con la _idea_ clave.\n> Cierre con CTA",
    "hashtags": ["#Ventas", "#Negocios", "#Emprender"],
    "primer_comentario": "**Comenta** tu caso y usa `esto` hoy.",
    "youtube_title": "# Como *vender* mas sin seguidores",
    "youtube_description": "Aprende **el sistema**.\n[Mira la guia](https://ejemplo.com/guia)",
    "post_x": "El alcance no paga: la **conversion** manda.",
    "ganchos": ["**Uno** directo.", "> Dos citado.", "`Tres` tecnico."],
}

MARKDOWN_LONGFORM_JSON = {
    "hilo_x": ["1/2 __Gancho__ del hilo.", "2/2 Cierre con [enlace](https://x.com/p)."],
    "articulo_x": "## Titulo\n\n**Parrafo** del articulo con `codigo`.\n\n### Sub\n\nFin.",
    "linkedin": "Primera **linea**.\n\n> Cita del video.\n\nCierre.",
    "newsletter": {
        "asuntos": ["*Asunto* A", "__Asunto__ B", "## Asunto C"],
        "cuerpo": "```\nCuerpo de la newsletter.\n```",
    },
    "blog": "## Titulo\n\nIntro con [guia](https://ejemplo.com).\n\n## H2\n\n**Desarrollo.**",
}


# --- fixtures / helpers ---------------------------------------------------

def _write_transcript(run_dir):
    words = []
    t = 0.0
    for w in "hoy te explico como vender mas sin necesitar mas seguidores".split():
        words.append({"text": w, "type": "word", "start": t, "end": t + 0.3})
        t += 0.3
    (run_dir / "transcript.json").write_text(
        json.dumps({"words": words}, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def ctx(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_transcript(run_dir)
    return FormatContext(video_id="9101_short", run_dir=run_dir, out_dir=out_dir)


def _install_agent(monkeypatch, stdout, *, returncode=0, stderr=""):
    """Monkeypatch the CLI resolver + runner; capture the call kwargs."""
    captured = {}

    def fake_run_agent_code(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(texts, "resolve_agent_cmd", lambda *a, **k: "claude")
    monkeypatch.setattr(texts, "run_agent_code", fake_run_agent_code)
    return captured


def _read(out_dir, name):
    return (out_dir / name).read_text(encoding="utf-8")


# --- produce_social -------------------------------------------------------

def test_social_writes_all_five_files(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON, ensure_ascii=False))
    paths = texts.produce_social(ctx)
    names = {p.name for p in paths}
    assert names == {
        "caption.txt", "primer_comentario.txt", "youtube_short.txt",
        "post_x.txt", "ganchos.txt",
    }
    for p in paths:
        assert p.exists()


def test_caption_appends_hashtags_at_end(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON, ensure_ascii=False))
    texts.produce_social(ctx)
    assert _read(ctx.out_dir, "caption.txt") == (
        "Gancho que frena el scroll.\nCuerpo con la idea.\nCierre con CTA."
        "\n\n#Ventas #Negocios #Emprender"
    )


def test_youtube_short_sections(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON, ensure_ascii=False))
    texts.produce_social(ctx)
    assert _read(ctx.out_dir, "youtube_short.txt") == (
        "TÍTULO:\nComo vender mas sin mas seguidores\n\n"
        "DESCRIPCIÓN:\nAprende el sistema en 60 segundos.\nSigueme para mas.\n"
    )


def test_ganchos_numbered(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON, ensure_ascii=False))
    texts.produce_social(ctx)
    assert _read(ctx.out_dir, "ganchos.txt") == (
        "1. Gancho alternativo uno.\n"
        "2. Gancho alternativo dos.\n"
        "3. Gancho alternativo tres.\n"
    )


def test_social_raw_strings(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON, ensure_ascii=False))
    texts.produce_social(ctx)
    assert _read(ctx.out_dir, "primer_comentario.txt") == SOCIAL_JSON["primer_comentario"]
    assert _read(ctx.out_dir, "post_x.txt") == SOCIAL_JSON["post_x"]


def test_social_partial_keys_skips_missing(ctx, monkeypatch):
    partial = {
        "caption": "Solo caption.",
        "hashtags": ["#Uno", "#Dos", "#Tres"],
        "post_x": "Un tweet.",
        # no primer_comentario, no youtube_*, no ganchos
    }
    _install_agent(monkeypatch, json.dumps(partial, ensure_ascii=False))
    paths = texts.produce_social(ctx)
    names = {p.name for p in paths}
    assert names == {"caption.txt", "post_x.txt"}
    assert not (ctx.out_dir / "youtube_short.txt").exists()
    assert not (ctx.out_dir / "ganchos.txt").exists()
    assert not (ctx.out_dir / "primer_comentario.txt").exists()


def test_caption_without_hashtags_has_no_dangling_gap(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps({"caption": "Solo caption."}, ensure_ascii=False))
    texts.produce_social(ctx)
    assert _read(ctx.out_dir, "caption.txt") == "Solo caption."


# --- produce_longform -----------------------------------------------------

def test_longform_writes_all_five_files(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(LONGFORM_JSON, ensure_ascii=False))
    paths = texts.produce_longform(ctx)
    names = {p.name for p in paths}
    assert names == {
        "hilo_x.txt", "articulo_x.txt", "linkedin.txt",
        "newsletter.txt", "blog.txt",
    }


def test_hilo_joined_with_separator(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(LONGFORM_JSON, ensure_ascii=False))
    texts.produce_longform(ctx)
    assert _read(ctx.out_dir, "hilo_x.txt") == (
        "1/3 Gancho del hilo.\n\n---\n\n2/3 Punto intermedio.\n\n---\n\n3/3 Cierre del hilo."
    )


def test_newsletter_format(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(LONGFORM_JSON, ensure_ascii=False))
    texts.produce_longform(ctx)
    assert _read(ctx.out_dir, "newsletter.txt") == (
        "ASUNTOS:\n- Asunto A\n- Asunto B\n- Asunto C\n\n"
        "CUERPO:\nCuerpo de la newsletter con la idea del video.\n"
    )


def test_longform_raw_strings(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(LONGFORM_JSON, ensure_ascii=False))
    texts.produce_longform(ctx)
    assert _read(ctx.out_dir, "articulo_x.txt") == LONGFORM_JSON["articulo_x"]
    assert _read(ctx.out_dir, "linkedin.txt") == LONGFORM_JSON["linkedin"]
    assert _read(ctx.out_dir, "blog.txt") == LONGFORM_JSON["blog"]


# --- parsing robustness ---------------------------------------------------

def test_fence_wrapped_json_parses(ctx, monkeypatch):
    fenced = "Aqui tienes el JSON:\n```json\n" + json.dumps(SOCIAL_JSON) + "\n```\nListo."
    _install_agent(monkeypatch, fenced)
    paths = texts.produce_social(ctx)
    assert len(paths) == 5


def test_trailing_extra_brace_tolerated(ctx, monkeypatch):
    # opus occasionally appends a stray closing brace after the object on long
    # outputs; raw_decode must parse the first complete object and ignore it.
    _install_agent(monkeypatch, json.dumps(LONGFORM_JSON, ensure_ascii=False) + "}")
    paths = texts.produce_longform(ctx)
    assert len(paths) == 5


def test_trailing_prose_after_object_tolerated(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON) + "\n\nEspero que te sirva.")
    paths = texts.produce_social(ctx)
    assert len(paths) == 5


def test_agent_nonzero_exit_raises(ctx, monkeypatch):
    _install_agent(monkeypatch, "", returncode=1, stderr="boom")
    with pytest.raises(Exception):
        texts.produce_social(ctx)


def test_unparseable_json_raises(ctx, monkeypatch):
    _install_agent(monkeypatch, "no hay ningun objeto json aqui")
    with pytest.raises(Exception):
        texts.produce_longform(ctx)


def test_missing_transcript_raises(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ctx = FormatContext(video_id="9101_short", run_dir=run_dir, out_dir=out_dir)
    _install_agent(monkeypatch, json.dumps(SOCIAL_JSON))
    with pytest.raises(Exception):
        texts.produce_social(ctx)


# --- grounding message ----------------------------------------------------

def test_message_grounded_with_plans(ctx, monkeypatch):
    (ctx.run_dir / "carousel").mkdir()
    (ctx.run_dir / "carousel" / "Carousel_Plan.json").write_text(
        json.dumps({
            "title": "Como vender mas",
            "slides": [
                {"kind": "hero", "title": "Como vender mas", "eyebrow": "El sistema"},
                {"kind": "item", "heading": "El alcance no paga", "body": "Sin conversion no sirve.", "index": 1, "total": 2},
                {"kind": "item", "heading": "Construye un sistema", "body": "", "index": 2, "total": 2},
            ],
        }, ensure_ascii=False), encoding="utf-8"
    )
    (ctx.run_dir / "quotes").mkdir()
    (ctx.run_dir / "quotes" / "Quote_Plan.json").write_text(
        json.dumps({"quotes": [{"text": "El alcance no paga."}, {"text": "Construye un sistema."}]},
                   ensure_ascii=False), encoding="utf-8"
    )
    captured = _install_agent(monkeypatch, json.dumps(SOCIAL_JSON))
    texts.produce_social(ctx)
    msg = captured["initial_message"]
    assert "<TRANSCRIPT>" in msg and "hoy te explico como vender mas" in msg
    assert "<TITULO>" in msg and "Como vender mas" in msg
    assert "<PUNTOS>" in msg and "El alcance no paga" in msg
    assert "<CITAS>" in msg and "Construye un sistema." in msg
    # exact agent knobs mirror the carousel orchestrator
    assert captured["model"] == "claude-opus-4-8"
    assert captured["effort"] == "medium"
    assert captured["tools"] == []
    assert captured["permission_mode"] is None
    assert captured["max_turns"] == 12
    assert captured["timeout_seconds"] == 300


def test_message_without_optional_plans(ctx, monkeypatch):
    captured = _install_agent(monkeypatch, json.dumps(LONGFORM_JSON))
    texts.produce_longform(ctx)
    msg = captured["initial_message"]
    assert "<TRANSCRIPT>" in msg
    assert "<TITULO>" not in msg
    assert "<PUNTOS>" not in msg
    assert "<CITAS>" not in msg


# --- _strip_markdown: markdown is removed -----------------------------------

@pytest.mark.parametrize("raw, expected", [
    # headings (1-6 hashes, only at line start, space required)
    ("## Titulo\n\nParrafo.", "Titulo\n\nParrafo."),
    ("# Uno\n### Tres\n###### Seis", "Uno\nTres\nSeis"),
    ("   ## Indentado", "Indentado"),
    # bold / italics (>= 2 chars wrapped)
    ("**fuerte** y normal", "fuerte y normal"),
    ("__fuerte__ y normal", "fuerte y normal"),
    ("una *palabra* clave", "una palabra clave"),
    ("una _palabra_ clave", "una palabra clave"),
    ("**dos palabras** juntas", "dos palabras juntas"),
    # inline code and fences
    ("usa `este comando` hoy", "usa este comando hoy"),
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ("antes\n```\ncontenido\n```\ndespues", "antes\ncontenido\ndespues"),
    # links
    ("lee [la guia](https://ejemplo.com/g) hoy", "lee la guia (https://ejemplo.com/g) hoy"),
    ("lee [la guia]() hoy", "lee la guia hoy"),
    ("![alt de imagen](https://e.com/i.png)", "alt de imagen (https://e.com/i.png)"),
    # blockquotes, incl. nested and quoted headings
    ("> cita textual", "cita textual"),
    ("> > doble cita", "doble cita"),
    ("> ## Titulo citado", "Titulo citado"),
    # combinations
    ("## T\n\n**a b** con `c` y [d](https://e.f)", "T\n\na b con c y d (https://e.f)"),
])
def test_strip_markdown_removes_syntax(raw, expected):
    assert texts._strip_markdown(raw) == expected


# --- _strip_markdown: legitimate prose is never mangled ----------------------

@pytest.mark.parametrize("text", [
    "Texto plano sin nada especial.",
    "Linea uno\n\nLinea dos con parrafos.",
    "1. Primero\n2. Segundo\n- Un guion",
    "TÍTULO:\nMi titulo literal",
    "un asterisco * suelto",
    "3 * 4 = 12 y 2 ** 8 tambien",
    "x**2 + y**2 es una potencia",
    "2*3*4 sin espacios",
    "snake_case_variable queda igual",
    "_privado sin cierre",
    "*a* corto queda igual",  # < 2 chars wrapped: left alone
    "#Ventas #Negocios",  # hashtags: no space after #
    "####### siete almohadillas",  # not a markdown heading level
    "el CTR > 2% es bueno",
    ">100 leads al mes",  # no space after > at line start
    "escribe city_id y user_name",
])
def test_strip_markdown_preserves_plain_text(text):
    assert texts._strip_markdown(text) == text


# --- producers scrub markdown before writing --------------------------------

MD_TOKENS = ("## ", "**", "```", "](")


def test_social_markdown_payload_writes_clean_files(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(MARKDOWN_SOCIAL_JSON, ensure_ascii=False))
    paths = texts.produce_social(ctx)
    assert len(paths) == 5
    for p in paths:
        content = p.read_text(encoding="utf-8")
        for token in MD_TOKENS:
            assert token not in content, f"{p.name} still contains {token!r}"
    assert _read(ctx.out_dir, "caption.txt") == (
        "Gancho fuerte\nCuerpo con la idea clave.\nCierre con CTA"
        "\n\n#Ventas #Negocios #Emprender"  # hashtags line untouched
    )
    assert _read(ctx.out_dir, "youtube_short.txt") == (
        "TÍTULO:\nComo vender mas sin seguidores\n\n"
        "DESCRIPCIÓN:\nAprende el sistema.\nMira la guia (https://ejemplo.com/guia)\n"
    )
    assert _read(ctx.out_dir, "primer_comentario.txt") == "Comenta tu caso y usa esto hoy."
    assert _read(ctx.out_dir, "post_x.txt") == "El alcance no paga: la conversion manda."
    assert _read(ctx.out_dir, "ganchos.txt") == (
        "1. Uno directo.\n2. Dos citado.\n3. Tres tecnico.\n"
    )


def test_longform_markdown_payload_writes_clean_files(ctx, monkeypatch):
    _install_agent(monkeypatch, json.dumps(MARKDOWN_LONGFORM_JSON, ensure_ascii=False))
    paths = texts.produce_longform(ctx)
    assert len(paths) == 5
    for p in paths:
        content = p.read_text(encoding="utf-8")
        for token in MD_TOKENS:
            assert token not in content, f"{p.name} still contains {token!r}"
    assert _read(ctx.out_dir, "hilo_x.txt") == (
        "1/2 Gancho del hilo.\n\n---\n\n2/2 Cierre con enlace (https://x.com/p)."
    )
    assert _read(ctx.out_dir, "articulo_x.txt") == (
        "Titulo\n\nParrafo del articulo con codigo.\n\nSub\n\nFin."
    )
    assert _read(ctx.out_dir, "linkedin.txt") == (
        "Primera linea.\n\nCita del video.\n\nCierre."
    )
    assert _read(ctx.out_dir, "newsletter.txt") == (
        "ASUNTOS:\n- Asunto A\n- Asunto B\n- Asunto C\n\n"
        "CUERPO:\nCuerpo de la newsletter.\n"
    )
    assert _read(ctx.out_dir, "blog.txt") == (
        "Titulo\n\nIntro con guia (https://ejemplo.com).\n\nH2\n\nDesarrollo."
    )
