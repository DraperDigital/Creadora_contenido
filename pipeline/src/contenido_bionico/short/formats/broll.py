"""B-roll photo library: list operator photos, pick a stable rotation per run,
and cut the person out of a photo (rembg).

Photos are operator-managed content in `<repo_root>/broll_library/` (gitignored).
Selection is an unbiased per-(run, purpose) SHUFFLE (`_shuffled`): every format /
slot draws its own uniformly-shuffled order of the WHOLE library, so slots differ,
formats don't collide, the full library is used with no positional bias, and
consecutive runs never overlap — yet it stays reproducible per (run, purpose) so
an edit re-render reproduces the same media. No agent is involved: filenames carry
no visual signal, so an LLM could not pick better than an unbiased shuffle.

`person_cutout` runs rembg behind a guarded import (missing rembg / any failure
degrades to `None`, never raises) and caches the alpha PNG as a sidecar NEXT TO
the source photo (`<photo>.cutout.png`), so the cut is computed once and shared
across every run and every format that needs it.
"""
from __future__ import annotations

import random
import re
import zlib
from pathlib import Path
from typing import Callable

from contenido_bionico.shared.config import REPO_ROOT
from contenido_bionico.shared.run_ids import run_number


def _pool_seed(ctx, key: str) -> int:
    """A stable-but-varied seed for shuffling a media pool.

    Deterministic across processes (so an edit re-render reproduces the same
    pick — `zlib.crc32` is stable, unlike the salted built-in `hash`), yet
    distinct per RUN and per KEY. That is what kills every repeat the linear
    rotation caused: different runs get an entirely different order (no
    shift-by-one overlap), and different formats/slots (`key`) draw from a
    different order (no two formats land on the same window). `key` names the
    pool + purpose, e.g. ``"photos:carrusel"``.
    """
    try:
        rn = int(run_number(ctx.video_id))
    except (ValueError, AttributeError, TypeError):
        rn = 0
    return (rn * 0x9E3779B1 + zlib.crc32(str(key).encode("utf-8"))) & 0xFFFFFFFF


def _shuffled(items: list, ctx, key: str) -> list:
    """A per-(run, key) UNIFORM shuffle of `items` — unbiased (every item equally
    likely in every position, so the whole library is used, not a front window),
    varied per run and per key, and reproducible for a given (run, key)."""
    deck = list(items)
    random.Random(_pool_seed(ctx, key)).shuffle(deck)
    return deck

BROLL_DIR = REPO_ROOT / "broll_library"
_PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_MIN_PHOTO_BYTES = 10 * 1024
# Cutout sidecars live in broll_library beside their source photo and carry a
# .png suffix, so `list_photos` must skip them or they'd be treated as photos.
CUTOUT_SUFFIX = ".cutout.png"


def list_photos() -> list[Path]:
    """Operator photos in `broll_library/` (sorted).

    Keeps only real image files: suffix in {.jpg,.jpeg,.png,.webp}, larger than
    10KB (drops icons/thumbnails/corrupt stubs), and excludes the
    `<photo>.cutout.png` sidecars this module writes. `[]` when the dir is
    missing.
    """
    if not BROLL_DIR.is_dir():
        return []
    photos: list[Path] = []
    for p in BROLL_DIR.iterdir():
        if not p.is_file():
            continue
        if p.name.endswith(CUTOUT_SUFFIX):
            continue
        if p.suffix.lower() not in _PHOTO_SUFFIXES:
            continue
        try:
            if p.stat().st_size <= _MIN_PHOTO_BYTES:
                continue
        except OSError:
            continue
        photos.append(p)
    return sorted(photos)


def select_photos(ctx, n: int, purpose: str) -> list[Path]:
    """`n` DISTINCT photos for `purpose`, from a per-(run, purpose) uniform shuffle
    of the WHOLE library.

    Every slot in the result differs; every format/`purpose` draws its OWN set
    (so the cita PNG and the cita mp4, or two photo carousels, never land on the
    same backgrounds); the full library is used with no positional bias; and
    consecutive runs get an entirely different order (no more shift-by-one
    overlap). Stable per (run, purpose) so an edit re-render reproduces the same
    photos. Returns fewer than `n` only when the library itself has fewer photos
    (then it cycles distinct passes so the whole pool is used before any repeat).
    """
    if n <= 0:
        return []
    photos = list_photos()
    if not photos:
        return []
    # `deck[:n]` = up to `n` DISTINCT photos from the shuffle; when the library
    # has fewer than `n` it returns all of them (shuffled) — you can't have more
    # distinct photos than exist.
    return _shuffled(photos, ctx, f"photos:{purpose}")[:n]


# --- pre-made cutout pairs (poster foto variants) ------------------------------
# Operators drop matched layer pairs in `broll_library/cutouts/`: a full opaque
# background PLATE + the SAME frame with the person cut out (transparent PNG).
# The poster `_foto` variants sandwich the giant type between them, so it sits
# behind the person but in front of the scene. Naming convention (operator-
# instructed): `background_N` always matches `foreground_N`. Also accepts a
# word suffix (`standing back` + `standing front`). The pair KEY is the name
# with the role word (background/foreground/front/back/...) removed; the alpha
# channel — not the name — decides which file is the foreground. Cutouts are
# NEVER generated from library photos: only the operator's matched pairs are used.
CUTOUTS_DIRNAME = "cutouts"
_BG_SUFFIX_TOKENS = ("back", "bg", "background", "base", "plate", "fondo")
_FG_SUFFIX_TOKENS = ("front", "fg", "foreground", "cutout", "persona", "recorte")
_ROLE_TOKENS = frozenset(_BG_SUFFIX_TOKENS + _FG_SUFFIX_TOKENS)


def _cutouts_dir() -> Path:
    return BROLL_DIR / CUTOUTS_DIRNAME


def _has_alpha(path: Path) -> bool:
    """True when `path` decodes to an image with a real transparency channel.

    Guarded: any missing-Pillow / decode failure returns False (treated as an
    opaque background), so a broken file never crashes selection.
    """
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA"):
                return im.getchannel("A").getextrema()[0] < 255
            return im.mode == "P" and "transparency" in im.info
    except Exception:  # noqa: BLE001 — any failure degrades to "opaque"
        return False


def _pair_key(name: str) -> str:
    """The pairing key of a cutout/background filename: the stem with every
    role word (background/foreground/front/back/fg/bg/...) removed, tokenizing on
    space / underscore / hyphen. So `background_1` and `foreground_1` both key to
    `1`, and `standing back` / `standing front` both key to `standing`."""
    stem = Path(name).stem.strip().lower()
    tokens = [t for t in re.split(r"[\s_\-]+", stem) if t and t not in _ROLE_TOKENS]
    return " ".join(tokens)


def list_cutout_pairs() -> list[tuple[Path | None, Path]]:
    """`(background, foreground)` pairs from `broll_library/cutouts/` (sorted).

    Foregrounds are the transparent PNGs; each is matched to the opaque plate
    sharing its pair-key (`foreground_1` <-> `background_1`), else `None` for a
    lone cutout the caller can render on a solid field. `[]` when the folder is
    missing or holds no transparent cutout.
    """
    d = _cutouts_dir()
    if not d.is_dir():
        return []
    imgs: list[Path] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _PHOTO_SUFFIXES:
            continue
        try:
            if p.stat().st_size <= _MIN_PHOTO_BYTES:
                continue
        except OSError:
            continue
        imgs.append(p)
    foregrounds = [p for p in imgs if _has_alpha(p)]
    backgrounds = [p for p in imgs if not _has_alpha(p)]
    pairs: list[tuple[Path | None, Path]] = []
    for fg in foregrounds:
        key = _pair_key(fg.name)
        match = next((bg for bg in backgrounds if _pair_key(bg.name) == key), None)
        pairs.append((match, fg))
    return pairs


def select_cutout_pair_for(ctx, index: int) -> tuple[Path, Path] | None:
    """The `index`-th COMPLETE `(background, foreground)` cutout pair (0-based)
    from a per-run uniform shuffle, so sibling posters each get a DIFFERENT
    `background_N`/`foreground_N` combo even when their text is the same, with no
    positional bias and full variation across runs. Wraps when the folder holds
    fewer complete pairs than requested. None when it holds no complete matched
    pair (lone cutouts are skipped).
    """
    pairs = [(bg, fg) for bg, fg in list_cutout_pairs() if bg is not None]
    if not pairs:
        return None
    deck = _shuffled(pairs, ctx, "cutout_pairs")
    return deck[index % len(deck)]


def select_cutout_pair(ctx) -> tuple[Path, Path] | None:
    """One COMPLETE `(background, foreground)` cutout pair for this run (the first
    in the run's rotation), or None. See `select_cutout_pair_for` for the
    index-aware selector the poster stack uses to vary the pair per layout."""
    return select_cutout_pair_for(ctx, 0)


def list_subject_cutouts() -> list[Path]:
    """All transparent (alpha) cutouts in `broll_library/cutouts/` (sorted).

    These are the foreground people the poster formats composite over a designed
    background — the plate is not used. `[]` when the folder holds none."""
    return [fg for _bg, fg in list_cutout_pairs()]


def select_subject_cutout_for(ctx, index: int) -> Path | None:
    """The `index`-th foreground cutout for this run (0-based) from a per-run
    uniform shuffle, so sibling SOLID posters each composite a DIFFERENT person
    over the designed background, unbiased and varied across runs. Wraps when the
    folder holds fewer cutouts than requested. None when it holds none. Cutouts
    are never generated from library photos, so a run with none renders the
    poster type-only on its background."""
    cutouts = list_subject_cutouts()
    if not cutouts:
        return None
    # A DIFFERENT order than the cutout-pair deck, so solid + foto posters vary
    # independently instead of tracking the same rotation.
    deck = _shuffled(cutouts, ctx, "subjects")
    return deck[index % len(deck)]


def select_subject_cutout(ctx, out_png: Path | None = None) -> Path | None:
    """A transparent subject cutout for this run's posters (the first in the run's
    rotation), or None. `out_png` is accepted for call-site compatibility and
    ignored — the ready-made cutout is returned in place. See
    `select_subject_cutout_for` for the index-aware selector."""
    return select_subject_cutout_for(ctx, 0)


# --- designed backgrounds (carousel canvases) ---------------------------------
# Full-bleed canvases in `broll_library/backgrounds/` a carousel can render over.
# Naming convention (operator-instructed): a name containing `dark` is a DARK
# canvas and needs a LIGHT element/text palette; every other canvas is light and
# needs dark elements. The operator controls it purely by filename.
BACKGROUNDS_DIRNAME = "backgrounds"


def _backgrounds_dir() -> Path:
    return BROLL_DIR / BACKGROUNDS_DIRNAME


def list_backgrounds() -> list[Path]:
    """Usable background images in `broll_library/backgrounds/` (sorted). `[]`
    when the folder is missing or empty."""
    d = _backgrounds_dir()
    if not d.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _PHOTO_SUFFIXES:
            continue
        try:
            if p.stat().st_size <= _MIN_PHOTO_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def is_dark_background(path: Path) -> bool:
    """True when a background is a DARK canvas (its filename contains `dark`), so
    the caller flips to a light element/text palette."""
    return "dark" in Path(path).stem.lower()


def select_background(ctx, *, offset: int = 0) -> Path | None:
    """One background from a per-run uniform shuffle of `broll_library/
    backgrounds/`. `offset` (per carousel) walks the shuffled deck so several
    carousels in one run land on DIFFERENT canvases; unbiased and fully varied
    across runs. None when the folder is empty."""
    backs = list_backgrounds()
    if not backs:
        return None
    deck = _shuffled(backs, ctx, "backgrounds")
    return deck[offset % len(deck)]


def _load_rembg_remove() -> Callable[[bytes], bytes] | None:
    """Return `rembg.remove` or None when rembg/onnxruntime is unavailable.

    Isolated so tests can monkeypatch the single rembg dependency and so the
    guarded import lives in exactly one place.
    """
    try:
        from rembg import remove  # type: ignore
    except Exception:  # noqa: BLE001 — any import/runtime failure degrades to skip
        return None
    return remove


def person_cutout(photo: Path, out_png: Path) -> Path | None:
    """Alpha-PNG cutout of the person in `photo`, written to `out_png`.

    Caches the cut beside the source photo as `<photo>.cutout.png` (shared
    across runs); a cache at least as new as the photo is reused instead of
    re-running rembg. Returns `out_png` on success, `None` on any failure
    (rembg missing, decode/segmentation error, write error) — never raises, so
    b-roll formats degrade to skip.
    """
    photo = Path(photo)
    out_png = Path(out_png)
    cache = photo.parent / (photo.name + CUTOUT_SUFFIX)
    try:
        fresh = cache.exists() and cache.stat().st_mtime >= photo.stat().st_mtime
        if not fresh:
            remove = _load_rembg_remove()
            if remove is None:
                return None
            data = remove(photo.read_bytes())
            if not data:
                return None
            cache.write_bytes(data)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        if out_png.resolve() != cache.resolve():
            out_png.write_bytes(cache.read_bytes())
        return out_png
    except Exception:  # noqa: BLE001 — best-effort: any failure degrades to skip
        return None
