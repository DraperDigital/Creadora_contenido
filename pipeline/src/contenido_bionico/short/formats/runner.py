"""Formats stage: run every enabled, atom-satisfied FormatSpec best-effort,
then write `formats_manifest.json` and the per-category packs.

Invariant: this stage can NEVER fail its caller. Every producer runs inside a
per-key try/except (failures log to `runs/<id>/logs/formats/<key>.log` and the
stage continues); the caller (`build_short_extras`) additionally wraps the whole
entry. Renders are heavy, so the pool is deliberately small (2 workers).
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from contenido_bionico.shared import config
from contenido_bionico.shared.run_ids import run_kind, run_number
from contenido_bionico.shared.runtime.adaptive import cpu_workers
from contenido_bionico.short.formats import atoms, broll, registry

RUNS_DIR = config.REPO_ROOT / "runs"

# Shipping deliverables (produced by the existing V5-2 flow, not the formats
# stage) also get indexed so the dashboard can group EVERYTHING by category.
# (glob, category, kind, format, label) — Spanish labels, deduped by filename.
_SHIPPING: tuple[tuple[str, str, str, str, str], ...] = (
    ("final_*.mp4", "videos", "video", "final", "Video final"),
    ("quote_*.mp4", "citas", "quote", "quote", "Frase en video"),
    ("carrusel_slide*.png", "carruseles", "slide", "carrusel", "Carrusel"),
    ("caption.txt", "textos", "caption", "caption", "Descripción"),
)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _run_output_dir(video_id: str | int) -> Path | None:
    """`<output_short>/run_<n>/` for a short (created on demand), else None.

    Replicates `pipeline.run_output_dir` here to avoid importing `pipeline`
    (which imports this module) — no circular import.
    """
    if run_kind(video_id) != "short":
        return None
    base = config.configured_output_dir("short")
    if base is None:
        return None
    out = base / f"run_{run_number(video_id)}"
    out.mkdir(parents=True, exist_ok=True)
    return out


@dataclass
class FormatContext:
    """Lazy, cached view of a run's atoms for the producers.

    Accessors are methods (not properties) so the `AVAILABILITY_CHECKS` table
    can call them uniformly; each computes once and caches (thread-safe), so the
    heavy `voice()` encode runs at most once per run.
    """

    video_id: str
    run_dir: Path
    out_dir: Path | None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def for_run(cls, video_id: str | int) -> "FormatContext":
        return cls(
            video_id=str(video_id),
            run_dir=RUNS_DIR / str(video_id),
            out_dir=_run_output_dir(video_id),
        )

    def _cached(self, key: str, compute: Callable[[], Any]) -> Any:
        with self._lock:
            if key not in self._cache:
                self._cache[key] = compute()
            return self._cache[key]

    def transcript(self) -> dict | None:
        return self._cached("transcript", lambda: _read_json(self.run_dir / "transcript.json"))

    def voice(self) -> Path | None:
        return self._cached("voice", lambda: atoms.voice_track(self.run_dir))

    def carousel_plan(self) -> dict | None:
        return self._cached("carousel_plan", lambda: atoms.load_carousel_plan(self.run_dir))

    def quote_plan(self) -> dict | None:
        return self._cached("quote_plan", lambda: atoms.load_quote_plan(self.run_dir))

    def palette(self) -> dict:
        return self._cached("palette", lambda: atoms.palette(self.run_dir))

    def captions_props(self) -> dict | None:
        return self._cached("captions_props", lambda: _read_json(self.run_dir / "captions_props.json"))

    def captions_webm(self) -> Path | None:
        def _find() -> Path | None:
            p = self.run_dir / "captions.webm"
            return p if p.exists() else None
        return self._cached("captions_webm", _find)

    def source(self) -> Path | None:
        def _find() -> Path | None:
            p = self.run_dir / "source.mp4"
            return p if p.exists() else None
        return self._cached("source", _find)


# Atom availability gates: a spec runs only when every `requires` key is present.
# `style_tokens` is always satisfied (palette() seeds defaults); `broll` reads
# the operator photo library.
AVAILABILITY_CHECKS: dict[str, Callable[[FormatContext], bool]] = {
    "transcript": lambda c: c.transcript() is not None,
    "source": lambda c: c.source() is not None,
    "voice": lambda c: c.voice() is not None,
    "captions_webm": lambda c: c.captions_webm() is not None,
    "captions_props": lambda c: c.captions_props() is not None,
    "carousel_plan": lambda c: c.carousel_plan() is not None,
    "quote_plan": lambda c: c.quote_plan() is not None,
    "style_tokens": lambda c: True,
    "broll": lambda c: bool(broll.list_photos()),
}


def _run_one(ctx: FormatContext, spec: registry.FormatSpec) -> list[Path]:
    producer = registry.resolve_producer(spec)
    published = [Path(p) for p in (producer(ctx) or [])]
    print(f"[formats] {spec.key}: ok ({len(published)} archivos)", flush=True)
    return published


def _log_failure(ctx: FormatContext, spec: registry.FormatSpec, exc: BaseException) -> None:
    log_dir = ctx.run_dir / "logs" / "formats"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        (log_dir / f"{spec.key}.log").write_text(
            f"[formats] {spec.key} fallo: {type(exc).__name__}: {exc}\n\n{tb}",
            encoding="utf-8",
        )
    except OSError:
        pass
    print(f"[formats] {spec.key}: fallo ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)


def _write_manifest(ctx: FormatContext, produced: list[tuple[registry.FormatSpec, Path]]) -> dict:
    categories: dict[str, list[dict]] = {c: [] for c in registry.CATEGORIES}
    seen: set[str] = set()
    for spec, path in produced:
        name = path.name
        if name in seen:
            continue
        seen.add(name)
        categories[spec.category].append(
            {"file": name, "kind": spec.kind, "label": spec.label, "format": spec.key}
        )
    # Index the shipping deliverables too (deduped by filename).
    for glob, category, kind, fmt, label in _SHIPPING:
        for path in sorted(ctx.out_dir.glob(glob)):
            name = path.name
            if name in seen:
                continue
            seen.add(name)
            categories[category].append(
                {"file": name, "kind": kind, "label": label, "format": fmt}
            )
    manifest = {
        "version": 1,
        "video_id": str(ctx.video_id),
        "categories": {c: v for c, v in categories.items() if v},
    }
    (ctx.out_dir / "formats_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def run_formats_stage(
    video_id: str | int, *, toggles: dict[str, bool] | None = None
) -> list[Path]:
    """Run every enabled + atom-satisfied format, then publish manifest + packs.

    Returns the list of published deliverable paths (excluding manifest/packs).
    Best-effort throughout: a producer failure is logged and skipped, never
    raised.
    """
    toggles = dict(toggles or {})
    ctx = FormatContext.for_run(video_id)
    if ctx.out_dir is None:
        return []
    enabled = [
        s for s in registry.FORMATS.values()
        if s.default_on and toggles.get(registry.TOGGLE_FOR_CATEGORY[s.category], True)
    ]
    runnable = [
        s for s in enabled
        if all(AVAILABILITY_CHECKS[r](ctx) for r in s.requires)
    ]
    produced: list[tuple[registry.FormatSpec, Path]] = []
    if runnable:
        # Parallelism scales to the host (~70% of cores, RAM-capped); renders are
        # heavy, so cpu_workers keeps a low-memory machine from oversubscribing.
        with ThreadPoolExecutor(max_workers=cpu_workers()) as pool:
            futs = {pool.submit(_run_one, ctx, s): s for s in runnable}
            for fut in as_completed(futs):
                spec = futs[fut]
                try:
                    produced += [(spec, p) for p in (fut.result() or [])]
                except Exception as exc:  # noqa: BLE001 — best-effort invariant
                    _log_failure(ctx, spec, exc)
    # No pack_*.zip here: uploading zips alongside the individual files doubled
    # the upload weight for nothing. Packs are now zipped ON DEMAND by the cloud
    # Worker at download time (formats_manifest.json still drives the grouping).
    _write_manifest(ctx, produced)
    return [p for _, p in produced]
