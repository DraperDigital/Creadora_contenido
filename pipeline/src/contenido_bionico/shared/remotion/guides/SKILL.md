---
name: remotion-scene-guides
description: Pipeline-specific Remotion guides for authoring one 1080x1920 overlay scene.
metadata:
  tags: remotion, video, react, animation, vertical
---

# Remotion scene guides (this pipeline)

You are writing ONE self-contained `Scene.tsx` overlay for a 1080x1920 (9:16), 30 fps vertical short. There is no project scaffolding, no Studio, no CLI, no `Root.tsx` here — the orchestrator registers your module, injects props, renders it with alpha, and composites it over the talking-head. These guides replace the generic Remotion docs; anything not listed here is not available.

## Allowed import surface (hard whitelist)

- `react` — default import plus `useRef` / `useMemo` / `useLayoutEffect` only.
- `remotion` — named imports: `AbsoluteFill`, `useCurrentFrame`, `useVideoConfig`, `interpolate`, `spring`, `Easing`, `Sequence`, `Img`, `staticFile`.
- `@remotion/layout-utils` — `fitText` only.

NOTHING else is installed. `@remotion/media`, `@remotion/google-fonts`, `@remotion/transitions`, `@remotion/light-leaks`, `gsap`, `framer-motion`, `react-spring`, remote URLs — all of these fail the render with `Cannot find module` or a network error. Never `useState` / `useEffect` / `setTimeout` / `requestAnimationFrame` / CSS `transition` / `@keyframes` / Tailwind classes: all motion derives from `useCurrentFrame()`.

## Fonts

`'Poppins'` (weights 400 / 600 / 900) is loaded globally by the render tree. Just set `fontFamily: "'Poppins', sans-serif"`. Never load or name any other font.

## Core patterns

```tsx
const frame = useCurrentFrame();
const { fps, durationInFrames } = useVideoConfig();

// Every interpolate: clamp BOTH sides, ease with intent (see rules/timing.md)
const enter = interpolate(frame, [startF, startF + 18], [0, 1], {
  easing: Easing.bezier(0.16, 1, 0.3, 1),
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
});

// Springs for pops / icon entrances / emphasis (see rules/timing.md presets)
const pop = spring({ frame: frame - startF, fps, config: { damping: 200 } });

// Delay a subtree
<Sequence from={startF} layout="none">…</Sequence>

// Fit a headline to a width
const { fontSize } = fitText({ text, withinWidth: 900, fontFamily: "Poppins", fontWeight: "900" });
```

## Transparent background technique

The root is `<AbsoluteFill style={{ backgroundColor: "transparent" }}>` — the talking-head shows through every alpha=0 pixel. Fade ONE opaque neutral layer in over the full 1080x1920 frame, animate content on top of it, then follow the exit envelope: content out first, background holds alone, background fades out ending exactly at `durationInFrames`.

## Programmatic visuals

Build every visual with inline SVG / CSS / React — draw icons, devices, charts, and diagrams as SVG paths and shapes. The ONLY asset reference allowed is `staticFile("assets/<id>.png")` of a real bundled asset via `<Img>`; never a remote URL. Any SVG with radiating decoration (waves, rings, glow, particles) spans the full canvas: `<svg width="1080" height="1920" viewBox="0 0 1080 1920" style={{ overflow: "visible" }}>`. Worked data-viz example: [rules/assets/charts-bar-chart.tsx](rules/assets/charts-bar-chart.tsx).

## Rules (open only what THIS scene needs)

- [rules/vertical-design.md](rules/vertical-design.md) — 9:16 layout zones, safe margins, type scale, stagger/choreography, contrast.
- [rules/timing.md](rules/timing.md) — intent-labeled easing library + spring presets (when to use which curve).
- [rules/sequencing.md](rules/sequencing.md) — `<Sequence>` patterns: delay, limit duration, nesting, local frames.
- [rules/trimming.md](rules/trimming.md) — trim the start/end of an animation with negative `from`.
- [rules/transitions.md](rules/transitions.md) — beat-to-beat transitions built from `interpolate` (crossfade, wipe, scale, radial).
- [rules/text-animations.md](rules/text-animations.md) — typewriter and word-highlight patterns.
- [rules/measuring-text.md](rules/measuring-text.md) — `fitText` usage.
- [rules/measuring-dom-nodes.md](rules/measuring-dom-nodes.md) — last-resort DOM measurement without state.
- [rules/images.md](rules/images.md) — sizing/positioning bundled assets with `<Img>`.
