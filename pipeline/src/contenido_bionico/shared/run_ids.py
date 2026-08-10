"""Run-id helpers for visible long/short run folders."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from contenido_bionico.shared.ffmpeg import probe_dimensions


RunKind = Literal["long", "short"]

RUN_ID_RE = re.compile(r"^(?P<number>\d+)(?:_(?P<kind>long|short))?$")


def parse_run_id(value: str | int) -> tuple[int, RunKind | None]:
    text = str(value)
    match = RUN_ID_RE.match(text)
    if not match:
        # User-facing (pipeline surfaces this verbatim) — keep it in Spanish.
        raise ValueError(
            f"id de video no valido: {text!r}. Usa un numero (ej. 3) "
            "o un id completo como '3_long' o '3_short'."
        )
    kind = match.group("kind")
    return int(match.group("number")), kind if kind in {"long", "short"} else None


def run_number(value: str | int) -> int:
    number, _kind = parse_run_id(value)
    return number


def run_kind(value: str | int) -> RunKind | None:
    _number, kind = parse_run_id(value)
    return kind


def make_run_id(number: int, kind: RunKind) -> str:
    if number <= 0:
        # Reachable from user input via resolve_run_id (e.g. "0") — Spanish.
        raise ValueError(f"el numero de video debe ser positivo, recibi {number}")
    return f"{number}_{kind}"


def next_run_id(runs_dir: Path, kind: RunKind) -> str:
    """Reserve the next free id for one visible run kind.

    Numbering is independent per kind, so `1_long` and `1_short` can coexist.
    Legacy numeric folders still reserve their number for both kinds.

    The run folder is created here with os.mkdir so two runs starting at the
    same time can never claim the same id: the loser gets FileExistsError and
    moves on to the next candidate. Callers re-mkdir with exist_ok=True.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    existing: set[int] = set()
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            number, existing_kind = parse_run_id(child.name)
        except ValueError:
            continue
        if existing_kind is None or existing_kind == kind:
            existing.add(number)
    number = 1
    while True:
        if number not in existing:
            candidate = make_run_id(number, kind)
            try:
                os.mkdir(runs_dir / candidate)
            except FileExistsError:
                pass  # raced by a simultaneous run; try the next number
            else:
                return candidate
        number += 1


def resolve_run_id(value: str | int, runs_dir: Path, *, expected_kind: RunKind | None = None) -> str:
    """Resolve a user-supplied rerun id to an existing run folder when possible."""
    text = str(value)
    if (runs_dir / text).exists():
        return text
    number, kind = parse_run_id(text)
    if kind is not None:
        return text
    if expected_kind is not None:
        candidate = make_run_id(number, expected_kind)
        if (runs_dir / candidate).exists():
            return candidate
        return candidate
    candidates = [make_run_id(number, k) for k in ("long", "short") if (runs_dir / make_run_id(number, k)).exists()]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError(
            f"el id de video {number} es ambiguo; "
            f"usa {candidates[0]!r} o {candidates[1]!r}"
        )
    return text


# Back-compat alias: the implementation moved to shared/ffmpeg.py.
ffprobe_dimensions = probe_dimensions


def infer_video_kind(path: Path) -> RunKind:
    width, height = ffprobe_dimensions(path)
    return "short" if height > width else "long"
