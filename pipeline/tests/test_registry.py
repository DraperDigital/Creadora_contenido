"""Tests for Task 4 — the PARTS registry (part -> producer/intermediate/
rerender/respawn/dependents).

Pure-data assertions only. Must NOT import or execute any render/respawn
callable (`rerender_part`, `recompose`, `run_animate`,
`surgical_recut`, `generate_and_publish_carousel`,
`generate_and_publish_quotes`). This module only checks the registry's shape
and content against the plan.
"""
from contenido_bionico.short.change.registry import PARTS, Part, part_for, parts_for_target


# Dispatch tags `rerender_part` (Task 5) is expected to know. Kept local to
# this test (not imported from Task 5) so this task has zero dependency on
# code that doesn't exist yet.
KNOWN_RERENDER_TAGS = {"captions", "scene", "audio"}

# Respawn entry points named across the codebase / later tasks. Kept local
# for the same reason.
KNOWN_RESPAWN_NAMES = {
    "run_animate",
    "surgical_recut",
    "generate_and_publish_carousel",
    "generate_and_publish_quotes",
}


def test_registry_shape():
    assert {p.target for p in PARTS.values()} <= {"video", "carousel", "quotes"}
    assert {p.key for p in parts_for_target("video")} == {
        "captions",
        "scene",
        "camera",
        "audio",
        "cut",
    }


def test_every_part_key_matches_its_dict_key():
    for key, part in PARTS.items():
        assert part.key == key


def test_every_target_is_valid():
    for part in PARTS.values():
        assert part.target in {"video", "carousel", "quotes"}


def test_every_rerender_tag_is_known_or_none():
    for part in PARTS.values():
        assert part.rerender is None or part.rerender in KNOWN_RERENDER_TAGS


def test_every_respawn_name_is_known_or_none():
    for part in PARTS.values():
        assert part.respawn is None or part.respawn in KNOWN_RESPAWN_NAMES


def test_registry_dependents_are_empty():
    """Respawn helpers own their internal re-derivation now.

    In particular, surgical_recut edits the cut, remaps scenes, re-derives the
    affected deterministic pieces, recomposes, and publishes by itself. The
    change agent must not manually walk dependents after calling it.
    """
    assert all(part.dependents == () for part in PARTS.values())


def test_cut_respawn_is_surgical_recut():
    """The registry respawn value matches the pinpointed pipeline entry."""
    assert PARTS["cut"].respawn == "surgical_recut"


def test_deterministic_parts_have_no_respawn():
    """captions/camera/audio are deterministic producers; notes are handled
    by editing the per-run intermediate directly, never a respawn."""
    for key in ("captions", "camera", "audio"):
        assert PARTS[key].respawn is None


def test_intermediates_are_nonempty_tuples_of_str():
    for part in PARTS.values():
        assert isinstance(part.intermediates, tuple)
        assert len(part.intermediates) > 0
        assert all(isinstance(p, str) for p in part.intermediates)


def test_captions_intermediates_match_settled_task_2_3_paths():
    assert PARTS["captions"].intermediates == (
        "captions_props.json",
        "Captions_Style.json",
        "remotion/Captions.tsx",
    )


def test_cut_intermediates_include_full_intermediates_subdir():
    cut = set(PARTS["cut"].intermediates)
    assert cut == {
        "_intermediates/word_for_word.json",
        "_intermediates/edl_render.json",
    }


def test_part_for_returns_matching_part():
    assert part_for("captions") is PARTS["captions"]


def test_part_for_unknown_key_raises_keyerror():
    try:
        part_for("does-not-exist")
    except KeyError as exc:
        assert "does-not-exist" in str(exc)
    else:
        raise AssertionError("part_for should raise KeyError for unknown key")


def test_parts_for_target_carousel():
    assert {p.key for p in parts_for_target("carousel")} == {"carousel"}


def test_parts_for_target_quotes():
    assert {p.key for p in parts_for_target("quotes")} == {"quotes"}


def test_parts_are_frozen_dataclass_instances():
    part = PARTS["captions"]
    assert isinstance(part, Part)
    try:
        part.rerender = "changed"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("Part should be frozen (immutable)")
