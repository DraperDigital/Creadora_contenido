"""Registry of SHORT deliverable PARTS: producer, per-run intermediate(s),
re-render dispatch tag, respawn entry, and dependents.

Pure descriptor module — no side effects, no imports of the render/respawn
callables it names. The change agent (and `rerender_part` / `recompose`,
Tasks 5-6) read this to route a request to the right producer instead of
improvising multi-step recipes. Callables are referenced by NAME (string) on
purpose so this module stays import-free; the named pipeline entries
(`run_animate`, `surgical_recut`, …) are resolved by the change
agent at call time.

See docs/superpowers/plans/2026-07-07-lego-intermediates-change-arch.md
(Architecture / Registry section, and Task 4) for the design this mirrors.
"""
from dataclasses import dataclass, field

TARGETS = ("video", "carousel", "quotes")


@dataclass(frozen=True)
class Part:
    key: str
    target: str                      # "video" | "carousel" | "quotes"
    producer: str                    # human description
    intermediates: tuple[str, ...]   # run-dir-relative paths the change agent edits
    rerender: str | None             # rerender_part dispatch tag (None = manifest-only, needs recompose)
    respawn: str | None              # pipeline entry to re-invoke the producer with notes
    dependents: tuple[str, ...] = field(default_factory=tuple)  # parts to re-derive when this one changes


PARTS: dict[str, Part] = {
    "captions": Part(
        "captions",
        "video",
        "chunk_cues + Captions.tsx",
        ("captions_props.json", "Captions_Style.json", "remotion/Captions.tsx"),
        rerender="captions",
        respawn=None,
    ),
    "scene": Part(
        "scene",
        "video",
        "short_author + Scene.tsx",
        ("animations/<seg>/Scene.tsx",),
        rerender="scene",
        respawn="run_animate",
    ),
    "camera": Part(
        "camera",
        "video",
        "plan_camera (deterministic)",
        ("Short_Assembly.json:camera",),
        rerender=None,
        respawn=None,
    ),
    "audio": Part(
        "audio",
        "video",
        "build_audio_plan + audio_mix",
        ("Audio_Plan.json",),
        rerender="audio",
        respawn=None,
    ),
    "carousel": Part(
        "carousel",
        "carousel",
        "carousel_author",
        ("carousel/Carousel_Plan.json",),
        rerender=None,
        respawn="generate_and_publish_carousel",
    ),
    "quotes": Part(
        "quotes",
        "quotes",
        "quote_select",
        ("quotes/Quote_Plan.json",),
        rerender=None,
        respawn="generate_and_publish_quotes",
    ),
    "cut": Part(
        "cut",
        "video",
        "surgical cut editor (edits edl_render.json, no editor/reviewer loop)",
        (
            "_intermediates/edl_render.json",       # THE editable cut decision (kept source ranges)
            "_intermediates/word_for_word.json",    # reference: every word + source timecode (read-only)
        ),
        rerender=None,
        # Surgical: edit the existing cut, rebuild source/transcript deterministically,
        # then re-derive ONLY the captions + the animation(s) over the changed moment.
        # The respawn self-recomposes/publishes, so dependents is empty (it owns the
        # re-derivation) -- the change agent must NOT manually re-derive parts after it.
        respawn="surgical_recut",
        dependents=(),
    ),
}


def part_for(key: str) -> Part:
    """Look up a Part by key. Raises KeyError with the available keys if unknown."""
    try:
        return PARTS[key]
    except KeyError:
        raise KeyError(
            f"Unknown part {key!r}; available parts: {sorted(PARTS)}"
        ) from None


def parts_for_target(target: str) -> list[Part]:
    """All Parts whose `target` matches (e.g. 'video' excludes carousel/quotes)."""
    return [p for p in PARTS.values() if p.target == target]
