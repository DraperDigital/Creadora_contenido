"""Command-line entry point: `contenido-bionico [options] [media]`."""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

from contenido_bionico import pipeline
from contenido_bionico.shared import progress
from contenido_bionico.skill_installer import COMMUNITY_URL, SKILLS, install_bionico_skill, skill_command_list


# Subcommands dispatched by hand in main(); listed here so --help shows them
# and so a mistyped subcommand gets a suggestion instead of "file not found".
SUBCOMMANDS: dict[str, str] = {
    "status": "muestra el avance de un run (status <run_id>, o el ultimo).",
    "install-skill": "activa los skills de Bionico en tu asistente de IA.",
}

PIPELINE_FORMATS_LABEL = "MP4, MOV o M4V"


def _subcommand_epilog() -> str:
    width = max(len(name) for name in SUBCOMMANDS)
    lines = ["Subcomandos:"]
    for name, description in SUBCOMMANDS.items():
        lines.append(f"  contenido-bionico {name:<{width}}  {description}")
    lines.append("")
    lines.append("Ejemplos:")
    lines.append("  contenido-bionico mi-video.mp4 --short")
    return "\n".join(lines)


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contenido-bionico",
        description=(
            "Convierte un video hablado a camara (MP4) en un short (9:16) "
            "editado y animado."
        ),
        epilog=_subcommand_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "media",
        type=Path,
        nargs="?",
        help=(
            f"Archivo fuente {PIPELINE_FORMATS_LABEL}. "
            "Omitelo con un --*-run."
        ),
    )

    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--short",
        dest="fmt",
        action="store_const",
        const="short",
        help="Salida vertical 9:16 (predeterminado).",
    )
    parser.set_defaults(fmt=None)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--cut-only",
        dest="mode",
        action="store_const",
        const="cut",
        help="Corre solo la fase de corte/edicion.",
    )
    mode.add_argument(
        "--animate-only",
        dest="mode",
        action="store_const",
        const="animate",
        help="Transcribe y anima la fuente tal cual, sin cortar.",
    )
    parser.set_defaults(mode="full")

    parser.add_argument(
        "--animate-run",
        metavar="RUN_ID",
        help="Avanzado: repite la fase de animacion sobre un run existente.",
    )
    parser.add_argument(
        "--edit-run",
        metavar="RUN_ID",
        help=(
            "Edicion puntual: aplica --notes re-haciendo solo las escenas "
            "afectadas de un short ya producido."
        ),
    )
    parser.add_argument(
        "--change-run",
        metavar="RUN_ID",
        help=(
            "Cambio orquestado por IA: un agente interpreta --notes sobre el "
            "entregable indicado por --target de un short ya producido, forka el "
            "run y aplica solo lo pedido (musica, animaciones, carrusel, quotes...)."
        ),
    )
    parser.add_argument(
        "--target",
        choices=["video", "carousel", "quotes"],
        default=None,
        help="Con --change-run: el entregable que el usuario abrio (video|carousel|quotes).",
    )
    parser.add_argument(
        "--carousel-run",
        metavar="RUN_ID",
        help="Avanzado: (re)genera el carrusel de un short ya producido.",
    )
    parser.add_argument(
        "--quotes-run",
        metavar="RUN_ID",
        help="Avanzado: (re)genera los quote posts de un short ya producido.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help="Numero maximo de segmentos animados en paralelo (predeterminado: 4).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Rehace la fase de animacion desde cero, ignorando los planes y "
            "segmentos ya generados del run."
        ),
    )
    # Internal (cloud versioning): fork the run into a fresh id before applying
    # the --edit-run/--quotes-run/--carousel-run, so the edited version is a new
    # run and the original stays intact. Not meant for manual use.
    parser.add_argument("--fork", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--notes",
        metavar="TEXTO",
        default=None,
        help=(
            "Con --animate-run/--edit-run/--carousel-run/--quotes-run: "
            "instrucciones de cambios del usuario que los agentes deben aplicar "
            "al regenerar."
        ),
    )
    parser.add_argument(
        "--anim-quality",
        choices=["low", "mid", "high", "max"],
        default=None,
        help=(
            "Calidad de las animaciones del short (modelo/esfuerzo de los agentes "
            "author/repair). Aplica con --short y --animate-run."
        ),
    )
    parser.add_argument(
        "--no-captions",
        action="store_true",
        help=(
            "Omite los subtitulos del short. Aplica con --short, "
            "--animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help=(
            "Omite el movimiento de camara del short (queda estatica). Aplica "
            "con --short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-animations",
        action="store_true",
        help=(
            "Omite las escenas animadas de mitad de video del short. Aplica "
            "con --short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--anim-count",
        choices=["few", "default", "max"],
        default=None,
        help=(
            "Cantidad de escenas animadas del short. Aplica con --short, "
            "--animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-music",
        action="store_true",
        help=(
            "Omite la musica de fondo del short. Aplica con --short, "
            "--animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-sfx",
        action="store_true",
        help=(
            "Omite los efectos de sonido (SFX) del short. Aplica con --short, "
            "--animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-carousel",
        action="store_true",
        help=(
            "No genera el carrusel del short (entregable secundario). Aplica con "
            "--short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-quotes",
        action="store_true",
        help=(
            "No genera los quote posts del short (entregable secundario). Aplica "
            "con --short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-videos-extra",
        action="store_true",
        help=(
            "No genera los formatos de video extra del short (clip subtitulado, "
            "texto protagonista, pantalla dividida, video con fotos). Aplica con "
            "--short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-video-carruseles",
        action="store_true",
        help=(
            "No genera los video-carruseles del short. Aplica con --short, "
            "--animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-carruseles-extra",
        action="store_true",
        help=(
            "No genera las variantes de carrusel del short (números, checklist, "
            "mitos, editorial, etc.). Aplica con --short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-imagenes",
        action="store_true",
        help=(
            "No genera las imágenes estáticas del short (frases, infografías). "
            "Aplica con --short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-textos",
        action="store_true",
        help=(
            "No genera los textos del short (caption, hilo, blog, etc.). Aplica "
            "con --short, --animate-run y --edit-run."
        ),
    )
    parser.add_argument(
        "--no-posters",
        action="store_true",
        help=(
            "No genera los pósters del short (motivacional, sobre mí, editorial, "
            "lanzamiento). Aplica con --short, --animate-run y --edit-run."
        ),
    )
    return parser


def _suggest_subcommand(media: Path) -> str | None:
    """Closest known subcommand for a bare word that is not an existing file."""
    name = str(media)
    if os.sep in name or "/" in name or media.suffix:
        return None
    matches = difflib.get_close_matches(name.lower(), list(SUBCOMMANDS), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _media_format_error(media: Path, mode: str) -> str | None:
    """Spanish error message when the media extension is not supported, else None."""
    suffix = media.suffix.lower()
    shown = media.suffix or media.name
    if suffix not in pipeline.PIPELINE_VIDEO_SUFFIXES:
        return (
            f"error: formato de video no soportado: '{shown}'. "
            f"Este modo acepta {PIPELINE_FORMATS_LABEL}; "
            "convierte el archivo a MP4 (H.264) e intentalo de nuevo."
        )
    return None


def run_pipeline(argv: list[str]) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    fmt = args.fmt or "short"
    max_concurrency = 4 if args.max_concurrency is None else args.max_concurrency

    if args.notes is not None and not (
        args.animate_run is not None
        or args.edit_run is not None
        or args.carousel_run is not None
        or args.quotes_run is not None
        or args.change_run is not None
    ):
        print(
            "error: --notes solo aplica con --animate-run, --edit-run, "
            "--carousel-run, --quotes-run o --change-run.",
            file=sys.stderr,
        )
        return 2

    if args.target is not None and args.change_run is None:
        print("error: --target solo aplica con --change-run.", file=sys.stderr)
        return 2

    if args.fork and not (
        args.edit_run is not None
        or args.carousel_run is not None
        or args.quotes_run is not None
    ):
        print(
            "error: --fork solo aplica con --edit-run, --carousel-run o --quotes-run.",
            file=sys.stderr,
        )
        return 2

    feature_toggles_requested = (
        args.no_captions or args.no_camera or args.no_animations
        or args.no_music or args.no_sfx or args.no_carousel or args.no_quotes
        or args.no_videos_extra or args.no_video_carruseles
        or args.no_carruseles_extra or args.no_imagenes or args.no_textos
        or args.no_posters or args.anim_count is not None
    )

    if args.animate_run is not None and args.edit_run is not None:
        print("error: usa solo uno de --edit-run o --animate-run.", file=sys.stderr)
        return 2

    if args.animate_run is not None:
        if args.media is not None:
            print(
                "error: --animate-run no se combina con un archivo.",
                file=sys.stderr,
            )
            return 2
        if args.fmt is not None:
            print(
                "error: --short no aplica con --animate-run; "
                "el formato se detecta del run existente.",
                file=sys.stderr,
            )
            return 2

    if args.animate_run is not None:
        run_id = pipeline.resolve_existing_run_id(args.animate_run)
        if args.notes is not None and pipeline.run_kind(run_id) != "short":
            print("error: --notes solo aplica a runs short.", file=sys.stderr)
            return 2
        if args.anim_quality is not None and pipeline.run_kind(run_id) != "short":
            print("error: --anim-quality solo aplica a runs short.", file=sys.stderr)
            return 2
        if feature_toggles_requested and pipeline.run_kind(run_id) != "short":
            print(
                "error: --no-captions/--no-camera/--no-animations/"
                "--no-music/--no-sfx/--no-carousel/--no-quotes/"
                "--no-videos-extra/--no-video-carruseles/--no-carruseles-extra/"
                "--no-imagenes/--no-textos/--anim-count solo "
                "aplican a runs short.",
                file=sys.stderr,
            )
            return 2
        output = pipeline.run_animate(
            run_id,
            max_concurrency=max_concurrency,
            force=args.force,
            notes=args.notes,
            anim_quality=args.anim_quality,
            no_captions=args.no_captions,
            no_camera=args.no_camera,
            no_animations=args.no_animations,
            anim_count=args.anim_count,
            no_music=args.no_music,
            no_sfx=args.no_sfx,
            no_carousel=args.no_carousel,
            no_quotes=args.no_quotes,
            no_videos_extra=args.no_videos_extra,
            no_video_carruseles=args.no_video_carruseles,
            no_carruseles_extra=args.no_carruseles_extra,
            no_imagenes=args.no_imagenes,
            no_textos=args.no_textos,
            no_posters=args.no_posters,
        )
        if output is not None and output.exists():
            print(f"[pipeline] done: {output}", flush=True)
        return 0

    if args.edit_run is not None:
        if args.media is not None:
            print(
                "error: --edit-run no se combina con un archivo.",
                file=sys.stderr,
            )
            return 2
        if args.notes is None:
            print(
                "error: --edit-run necesita --notes con los cambios a aplicar.",
                file=sys.stderr,
            )
            return 2
        run_id = pipeline.resolve_existing_run_id(args.edit_run)
        if pipeline.run_kind(run_id) != "short":
            print(
                f"error: la edicion puntual solo aplica a shorts; '{run_id}' no es un short.",
                file=sys.stderr,
            )
            return 2
        # Versioned edit: fork the run so the edited version is a new run and the
        # original stays intact. The fork already carries the parent's carousel/
        # quotes unchanged, so skip rebuilding them (reuse the forked copies).
        if args.fork:
            run_id = pipeline.fork_run(run_id)
        output = pipeline.run_animate(
            run_id,
            max_concurrency=max_concurrency,
            force=False,
            notes=args.notes,
            anim_quality=args.anim_quality,
            edit=True,
            no_captions=args.no_captions,
            no_camera=args.no_camera,
            no_animations=args.no_animations,
            anim_count=args.anim_count,
            no_music=args.no_music,
            no_sfx=args.no_sfx,
            no_carousel=args.no_carousel,
            no_quotes=args.no_quotes,
            no_videos_extra=args.no_videos_extra,
            no_video_carruseles=args.no_video_carruseles,
            no_carruseles_extra=args.no_carruseles_extra,
            no_imagenes=args.no_imagenes,
            no_textos=args.no_textos,
            no_posters=args.no_posters,
            build_extras=not args.fork,
        )
        if output is not None and output.exists():
            print(f"[pipeline] done: {output}", flush=True)
        return 0

    if args.change_run is not None:
        if args.media is not None:
            print(
                "error: --change-run no se combina con un archivo.",
                file=sys.stderr,
            )
            return 2
        if not args.target:
            print(
                "error: --change-run necesita --target video|carousel|quotes.",
                file=sys.stderr,
            )
            return 2
        if args.notes is None or not args.notes.strip():
            print(
                "error: --change-run necesita --notes con el cambio solicitado.",
                file=sys.stderr,
            )
            return 2
        anim_opts = {
            "anim_quality": args.anim_quality,
            "no_captions": args.no_captions,
            "no_camera": args.no_camera,
            "no_animations": args.no_animations,
            "anim_count": args.anim_count,
            "no_music": args.no_music,
            "no_sfx": args.no_sfx,
            "no_carousel": args.no_carousel,
            "no_quotes": args.no_quotes,
        }
        try:
            pipeline.run_change_on_fork(args.change_run, args.target, args.notes, anim_opts=anim_opts)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"error: fallo el cambio: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.carousel_run is not None or args.quotes_run is not None:
        if args.media is not None:
            print(
                "error: --carousel-run/--quotes-run no se combinan con un archivo.",
                file=sys.stderr,
            )
            return 2
        rc = 0
        out_dir: Path | None = None
        run_id: str | None = None
        if args.carousel_run is not None:
            run_id = pipeline.resolve_existing_run_id(args.carousel_run)
            if pipeline.run_kind(run_id) != "short":
                print(
                    f"error: el carrusel solo aplica a shorts; '{run_id}' no es un short.",
                    file=sys.stderr,
                )
                return 2
            # Versioned edit: fork so only the carousel is regenerated on a new
            # run; the forked video + quotes are kept and the original is intact.
            if args.fork:
                run_id = pipeline.fork_run(run_id)
            try:
                result = pipeline.generate_and_publish_carousel(run_id, strict=True, notes=args.notes)
                out_dir = result if result is not None else out_dir
            except Exception as exc:  # noqa: BLE001
                print(f"error: fallo el carrusel de {run_id}: {exc}", file=sys.stderr)
                rc = 1
        if args.quotes_run is not None:
            run_id = pipeline.resolve_existing_run_id(args.quotes_run)
            if pipeline.run_kind(run_id) != "short":
                print(
                    f"error: los quote posts solo aplican a shorts; '{run_id}' no es un short.",
                    file=sys.stderr,
                )
                return 2
            # Versioned edit: fork so only the quotes are regenerated on a new
            # run; the forked video + carousel are kept and the original intact.
            if args.fork:
                run_id = pipeline.fork_run(run_id)
            try:
                result = pipeline.generate_and_publish_quotes(run_id, strict=True, notes=args.notes)
                out_dir = result if result is not None else out_dir
            except Exception as exc:  # noqa: BLE001
                print(f"error: fallaron los quote posts de {run_id}: {exc}", file=sys.stderr)
                rc = 1
        if rc == 0 and out_dir is not None:
            pipeline.refresh_run_caption(run_id)
            print(f"[pipeline] done: {out_dir}", flush=True)
        return rc

    media = args.media

    if media is None:
        parser.print_help()
        return 2
    if not media.exists():
        suggestion = _suggest_subcommand(media)
        if suggestion is not None:
            print(f"error: '{media}' no es un archivo ni un subcomando.", file=sys.stderr)
            print(
                f"¿Quisiste decir 'contenido-bionico {suggestion}'?",
                file=sys.stderr,
            )
        else:
            print(f"error: no existe el archivo: {media}", file=sys.stderr)
        return 2

    format_error = _media_format_error(media, args.mode)
    if format_error is not None:
        print(format_error, file=sys.stderr)
        return 2

    mode = args.mode

    if mode == "cut":
        if args.max_concurrency is not None:
            print(
                "aviso: --max-concurrency no tiene efecto con --cut-only; se ignora.",
                file=sys.stderr,
            )
        if args.force:
            print(
                "aviso: --force no tiene efecto con --cut-only; se ignora.",
                file=sys.stderr,
            )
        pipeline.run_cut(media, run_kind_override=fmt)
        return 0

    if mode == "animate":
        pipeline.run_animation_only(
            media,
            max_concurrency=max_concurrency,
            run_kind_override=fmt,
            force=args.force,
        )
        return 0

    # full: cut then animate (short)
    pipeline.run_short(
        media,
        max_concurrency=max_concurrency,
        force=args.force,
        anim_quality=args.anim_quality,
        no_captions=args.no_captions,
        no_camera=args.no_camera,
        no_animations=args.no_animations,
        anim_count=args.anim_count,
        no_music=args.no_music,
        no_sfx=args.no_sfx,
        no_carousel=args.no_carousel,
        no_quotes=args.no_quotes,
        no_videos_extra=args.no_videos_extra,
        no_video_carruseles=args.no_video_carruseles,
        no_carruseles_extra=args.no_carruseles_extra,
        no_imagenes=args.no_imagenes,
        no_textos=args.no_textos,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    # The Task-8 UTF-8 env (PYTHONUTF8/PYTHONIOENCODING) only covers agent
    # SUBPROCESSES; this outer CLI process itself can still start on a
    # legacy codepage (e.g. Windows cp1252) when stdout/stderr are piped or
    # redirected (dashboard/watcher). Reconfigure both streams to UTF-8 up
    # front so any non-ASCII in a summary/log (an arrow, an emoji, an
    # accented char) can never crash `print()` and cause a successful change
    # to be misreported as failed. `.reconfigure` exists on real TextIO
    # (Python 3.7+); the hasattr guard just protects a stream that has been
    # swapped for something else (e.g. in tests).
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    # Put this root process (and therefore every ffmpeg/node/agent child it
    # spawns, however deeply nested) into a kill-on-close job, so killing the
    # run reaps the whole tree instead of leaking orphan ffmpeg workers.
    # Windows Job Object under the hood; idempotent and a safe no-op where
    # unsupported (install_process_reaper never raises).
    try:
        from contenido_bionico.shared.runtime.process_reaper import install_process_reaper
    except ImportError:
        pass
    else:
        install_process_reaper()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "status":
        status_parser = argparse.ArgumentParser(prog="contenido-bionico status")
        status_parser.add_argument(
            "run_id",
            nargs="?",
            help="Run a inspeccionar. Si se omite, se revisa el run mas reciente.",
        )
        status_parser.add_argument("--json", action="store_true")
        args = status_parser.parse_args(argv[1:])
        return progress.print_status(args.run_id, as_json=args.json)
    if argv and argv[0] == "install-skill":
        install_parser = argparse.ArgumentParser(prog="contenido-bionico install-skill")
        install_parser.add_argument(
            "--target",
            choices=["auto", "all", "codex", "claude"],
            default="auto",
            help=(
                f"Donde instalar {skill_command_list()} "
                "(predeterminado: detectar los clientes soportados)."
            ),
        )
        args = install_parser.parse_args(argv[1:])
        installed = install_bionico_skill(args.target)
        if installed:
            print(f"Listo: {skill_command_list()} quedaron disponibles donde fue posible.")
            print("Skills instalados:")
            for skill in SKILLS:
                print(f"  /{skill.name} - {skill.description}")
            print(f"Comunidad Bionico para ayuda de implementacion, uso y personalizacion: {COMMUNITY_URL}")
        else:
            print("No encontre donde activar los skills de Bionico automaticamente.")
        return 0 if installed else 1
    if argv and argv[0] == "reclaim-run":
        # Internal (cloud versioning): reclaim a deleted version's local disk —
        # its runs/<id>/ working folder and output_short/run_<n>/ deliverables.
        # Best-effort; the version is already gone in the cloud.
        reclaim_parser = argparse.ArgumentParser(prog="contenido-bionico reclaim-run")
        reclaim_parser.add_argument("run_id")
        args = reclaim_parser.parse_args(argv[1:])
        pipeline.reclaim_run(args.run_id)
        return 0
    return run_pipeline(argv)


if __name__ == "__main__":
    sys.exit(main())
