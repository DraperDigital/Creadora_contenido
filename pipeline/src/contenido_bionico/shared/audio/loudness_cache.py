"""Cached EBU R128 loudness measurements for library audio files.

Measuring a file's integrated LUFS costs one full ffmpeg decode, so results
are cached in a small sidecar JSON next to the library's MANIFEST.json
(``audio_library/.loudness_cache.json``). MANIFEST.json itself is never
touched. Entries are keyed by the file's resolved absolute path and
invalidated when the file's size or mtime changes, so swapping an audio file
in place re-measures it automatically.

Everything here is best-effort: an unreadable/unwritable cache degrades to a
fresh measurement, and an unmeasurable file (no audio / near-silent) is cached
as null so it is not re-decoded on every run.
"""
from __future__ import annotations

import json
from pathlib import Path

from contenido_bionico.shared.ffmpeg import measure_lufs

CACHE_FILENAME = ".loudness_cache.json"
_CACHE_VERSION = 1


def _cache_path(library_root: Path | str | None) -> Path | None:
    if library_root is None:
        return None
    try:
        root = Path(library_root).resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    return root / CACHE_FILENAME


def _load_entries(cache_path: Path | None) -> dict:
    if cache_path is None or not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError covers JSON and UTF-8 decode errors
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_entries(cache_path: Path | None, entries: dict) -> None:
    if cache_path is None:
        return
    try:
        cache_path.write_text(
            json.dumps(
                {"version": _CACHE_VERSION, "entries": entries},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # cache write failures must never break a mix


def cached_lufs(
    media_path: Path | str,
    library_root: Path | str | None = None,
) -> float | None:
    """Integrated LUFS of `media_path`, cached in the library sidecar.

    Returns the measured value, or None when the file is missing or has no
    measurable audio. With `library_root=None` (or a nonexistent dir) the
    measurement still runs, just without caching.
    """
    try:
        path = Path(media_path).resolve()
        stat = path.stat()
    except OSError:
        return None

    cache_path = _cache_path(library_root)
    entries = _load_entries(cache_path)
    key = str(path)
    entry = entries.get(key)
    if (
        isinstance(entry, dict)
        and entry.get("size") == stat.st_size
        and entry.get("mtime") == stat.st_mtime
    ):
        value = entry.get("lufs")
        return float(value) if isinstance(value, (int, float)) else None

    value = measure_lufs(path)
    entries[key] = {"lufs": value, "size": stat.st_size, "mtime": stat.st_mtime}
    _save_entries(cache_path, entries)
    return value
