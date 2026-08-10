"""Parse and resolve author-selected Sound_Cues.json payloads.

This module intentionally does more than strict validation. Authors may ask
for a sound that is not in the local library yet; the resolver should not abort
the render. It resolves confident matches to concrete files and returns
unresolved requests for a missing-sounds report.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import Manifest, SfxEntry


class CueValidationError(RuntimeError):
    """The cue payload is structurally unusable."""


@dataclass(frozen=True)
class ResolvedCue:
    segment_id: int
    requested_sfx_name: str
    sfx_name: str
    offset_seconds: float
    gain_db: float
    intent: str
    file: Path
    match_type: str


@dataclass(frozen=True)
class UnresolvedCue:
    segment_id: int
    requested_sfx_name: str
    requested_sound: str
    offset_seconds: float
    gain_db: float | None
    intent: str
    reason: str
    nearest_candidates: tuple[str, ...]


@dataclass(frozen=True)
class CueResolution:
    segment_id: int
    resolved: tuple[ResolvedCue, ...]
    unresolved: tuple[UnresolvedCue, ...]
    warnings: tuple[str, ...]


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _norm(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.lower()))


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.lower()))


def _as_float(value: object, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CueValidationError(f"{label} must be a number, got {value!r}") from exc


def _candidate_text(entry: SfxEntry) -> str:
    return " ".join(
        [
            entry.name,
            entry.category,
            entry.when_to_use,
            " ".join(entry.tags),
        ]
    )


def _score(query: str, entry: SfxEntry) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    entry_tokens = _tokens(_candidate_text(entry))
    if not entry_tokens:
        return 0.0
    overlap = len(query_tokens & entry_tokens)
    return overlap / max(len(query_tokens), 1)


def _nearest(query: str, manifest: Manifest, limit: int = 3) -> tuple[str, ...]:
    scored = sorted(
        (
            (_score(query, entry), entry.name)
            for entry in manifest.sfx_by_name.values()
        ),
        key=lambda item: (-item[0], item[1]),
    )
    return tuple(name for score, name in scored[:limit] if score > 0)


def _resolve_exact_name(name: str, manifest: Manifest) -> SfxEntry | None:
    normalized = _norm(name)
    if not normalized:
        return None
    for entry in manifest.sfx_by_name.values():
        if _norm(entry.name) == normalized:
            return entry
    return None


def _resolve_entry(query: str, manifest: Manifest) -> tuple[SfxEntry | None, str, tuple[str, ...]]:
    exact = _resolve_exact_name(query, manifest)
    if exact is not None:
        return exact, "exact_name", ()
    scored = sorted(
        (
            (_score(query, entry), entry)
            for entry in manifest.sfx_by_name.values()
        ),
        key=lambda item: (-item[0], item[1].name),
    )
    if scored and scored[0][0] >= 0.5:
        return scored[0][1], "catalog_similarity", tuple(
            entry.name for score, entry in scored[1:4] if score > 0
        )
    return None, "unresolved", _nearest(query, manifest)


def _cue_query(raw: dict[str, Any]) -> str:
    """Build the semantic fallback query.

    `sfx_name` is a catalog id, so it is resolved directly before semantic
    matching. Including it here would dilute exact author choices with intent
    prose and can push the score below the confidence threshold.
    """
    parts = [
        str(raw.get("requested_sound") or ""),
        str(raw.get("sound") or ""),
        str(raw.get("intent") or ""),
        str(raw.get("rationale") or ""),
    ]
    return " ".join(p for p in parts if p.strip()).strip()


def resolve_cues_payload(
    payload: object,
    manifest: Manifest,
    *,
    segment_duration_seconds: float,
    library_root: Path | None = None,
) -> CueResolution:
    """Resolve a Sound_Cues payload without failing on missing library sounds."""
    if not isinstance(payload, dict):
        raise CueValidationError(f"Sound_Cues must be a JSON object, got {type(payload).__name__}")
    if "segment_id" not in payload:
        raise CueValidationError("Sound_Cues missing required key 'segment_id'")
    if "cues" not in payload:
        raise CueValidationError("Sound_Cues missing required key 'cues'")
    try:
        segment_id = int(payload["segment_id"])
    except (TypeError, ValueError) as exc:
        raise CueValidationError(
            f"segment_id must be an int, got {payload['segment_id']!r}"
        ) from exc
    raw_cues = payload["cues"]
    if not isinstance(raw_cues, list):
        raise CueValidationError(f"cues must be a JSON array, got {type(raw_cues).__name__}")

    root = (library_root or manifest.library_root).resolve()
    constraints = manifest.constraints
    resolved: list[ResolvedCue] = []
    unresolved: list[UnresolvedCue] = []
    warnings: list[str] = []

    for idx, raw in enumerate(raw_cues):
        ctx = f"cues[{idx}]"
        if not isinstance(raw, dict):
            warnings.append(f"{ctx}: ignored non-object cue ({type(raw).__name__})")
            continue
        sfx_name = str(raw.get("sfx_name") or "").strip()
        query = _cue_query(raw)
        requested_name = str(raw.get("sfx_name") or raw.get("requested_sound") or raw.get("sound") or "").strip()
        requested_sound = str(raw.get("requested_sound") or raw.get("sound") or query or requested_name).strip()
        intent = str(raw.get("intent") or raw.get("rationale") or "").strip()
        if not (sfx_name or query):
            warnings.append(f"{ctx}: ignored cue without sfx_name/requested_sound/intent")
            continue
        try:
            offset = _as_float(raw.get("offset_seconds"), f"{ctx}.offset_seconds")
        except CueValidationError as exc:
            warnings.append(str(exc))
            continue
        if offset < 0:
            warnings.append(f"{ctx}: offset_seconds {offset:.3f} below 0; clamped to 0")
            offset = 0.0
        if offset > segment_duration_seconds:
            warnings.append(
                f"{ctx}: offset_seconds {offset:.3f} exceeds duration "
                f"{segment_duration_seconds:.3f}; clamped to segment end"
            )
            offset = max(segment_duration_seconds, 0.0)

        if any(raw.get(key) is not None for key in ("gain_db", "volume_db", "db", "level_db")):
            warnings.append(
                f"{ctx}: per-cue gain fields are ignored; "
                "using MANIFEST.json sfx[].default_gain_db"
            )

        entry: SfxEntry | None = None
        match_type = "unresolved"
        nearest: tuple[str, ...] = ()
        if sfx_name:
            entry = _resolve_exact_name(sfx_name, manifest)
            if entry is not None:
                match_type = "exact_name"
            else:
                # Authors write {sfx_name, offset_seconds, intent}, so a bad
                # sfx_name is usually a near-miss of a catalog id (e.g.
                # "card_entry" for entry_card). Fuzzy-match the name itself
                # first: its few tokens score far above the confidence
                # threshold where long intent prose never would.
                warnings.append(
                    f"{ctx}: unknown sfx_name {sfx_name!r}; trying fuzzy match"
                )
                entry, match_type, nearest = _resolve_entry(sfx_name, manifest)
                if entry is None and query:
                    entry, match_type, query_nearest = _resolve_entry(query, manifest)
                    nearest = query_nearest or nearest
        elif query:
            entry, match_type, nearest = _resolve_entry(query, manifest)
        if entry is None:
            unresolved.append(
                UnresolvedCue(
                    segment_id=segment_id,
                    requested_sfx_name=requested_name,
                    requested_sound=requested_sound,
                    offset_seconds=offset,
                    gain_db=None,
                    intent=intent,
                    reason="no confident catalog match",
                    nearest_candidates=nearest,
                )
            )
            continue

        file_path = entry.absolute_path(root)
        if not file_path.exists():
            unresolved.append(
                UnresolvedCue(
                    segment_id=segment_id,
                    requested_sfx_name=requested_name or entry.name,
                    requested_sound=requested_sound or entry.when_to_use,
                    offset_seconds=offset,
                    gain_db=None,
                    intent=intent,
                    reason=f"matched {entry.name!r}, but file is missing: {entry.file}",
                    nearest_candidates=nearest,
                )
            )
            continue

        resolved.append(
            ResolvedCue(
                segment_id=segment_id,
                requested_sfx_name=requested_name,
                sfx_name=entry.name,
                offset_seconds=offset,
                gain_db=entry.default_gain_db,
                intent=intent,
                file=file_path,
                match_type=match_type,
            )
        )

    if len(resolved) > constraints.max_cues_per_segment:
        warnings.append(
            f"cues[]: {len(resolved)} resolved cues exceeds max "
            f"{constraints.max_cues_per_segment}; extra cues were dropped"
        )
        kept = sorted(resolved, key=lambda cue: cue.offset_seconds)[: constraints.max_cues_per_segment]
        resolved = list(kept)

    spacing = constraints.min_cue_spacing_seconds
    if spacing > 0 and len(resolved) >= 2:
        spaced: list[ResolvedCue] = []
        last_offset: float | None = None
        for cue in sorted(resolved, key=lambda item: item.offset_seconds):
            if last_offset is not None and cue.offset_seconds - last_offset < spacing:
                warnings.append(
                    f"dropped {cue.sfx_name}@{cue.offset_seconds:.3f}s; spacing below {spacing}s"
                )
                continue
            spaced.append(cue)
            last_offset = cue.offset_seconds
        resolved = spaced

    return CueResolution(
        segment_id=segment_id,
        resolved=tuple(sorted(resolved, key=lambda cue: cue.offset_seconds)),
        unresolved=tuple(unresolved),
        warnings=tuple(warnings),
    )


def validate_file(
    path: Path,
    manifest: Manifest,
    *,
    segment_duration_seconds: float,
) -> CueResolution:
    if not path.exists():
        raise CueValidationError(f"Sound_Cues file does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CueValidationError(f"Sound_Cues at {path} is not valid JSON: {exc}") from exc
    return resolve_cues_payload(
        payload,
        manifest,
        segment_duration_seconds=segment_duration_seconds,
    )
