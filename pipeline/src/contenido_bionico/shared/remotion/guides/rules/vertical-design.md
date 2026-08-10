---
name: vertical-design
description: 9:16 motion-design rules — layout zones, safe margins, type scale, choreography, contrast
metadata:
  tags: vertical, 9:16, layout, safe-zone, typography, choreography
---

# Designing for 1080x1920 (9:16)

The canvas is tall and narrow, watched on a phone at arm's length, with platform UI stacked over it. Design for that reality.

## Layout zones

```
y    0 –  200   top margin — breathing room, avoid settled content
y  200 –  700   HEADLINE ZONE — titles, hero numbers
y  700 – 1300   FOCAL ZONE — the central visual (diagram, object, chart)
y 1300 – 1536   SUPPORT ZONE — captions to the visual, secondary labels
y 1536 – 1920   PLATFORM UI — TikTok/Reels buttons, description, progress bar
```

- **Safe zone (HARD):** nothing SETTLES below y = 1536 — the bottom 20% is covered by platform UI. Elements may travel through it while entering/exiting.
- Side margins: keep settled text >= 60px from the left/right edges (readable column: x 60–1020).
- The focal element of each moment sits centered on **x = 540**; secondary elements may extend off-center.
- One focal point per moment. Vertical stacking is the natural composition: headline above, visual center, support below.

**Flow over coordinates.** Place these zones as a few absolutely positioned region containers and lay content out INSIDE them with `display: "flex"` containers (column/row) plus `gap` — never by absolute pixel coordinates. Anything that could visually touch (icon + label, label + badge) must be flex siblings, with `minWidth: 0` on text so it wraps; size headlines with `fitText` from `@remotion/layout-utils`. Absolute positioning is for region containers and full-bleed decor behind content only.

## Type scale (matches the scene contract)

| Role | Size |
| --- | --- |
| Giant hero | 180–220px |
| Headline | >= 120px |
| Body / labels | >= 56px |
| Smallest readable string | ~34px — NOTHING a viewer must read goes below this |
| Decorative microcopy | greeked bars (rounded rects), never real tiny words |

Font is always `'Poppins'` (400 / 600 / 900). Weight contrast (900 headline over 400 body) beats size contrast; use both. Keep lines short — 1–3 words per line reads best at this scale; never let a word split across lines.

## Stagger and choreography

A multi-beat scene is choreography, not a slideshow:

- Elements enter ONE AT A TIME on their spoken word (anchor to the word's `start` from `props.words`), not simultaneously. Sibling items (list rows, bars) stagger 3–6 frames apart.
- Lead with the focal element; supporting elements follow it, never precede it.
- While a new element enters, ease already-settled elements slightly (shift 20–40px, scale to 0.95, or dim to 0.7 opacity) to hand over focus.
- Between beats, use ONE transition pattern (see rules/transitions.md) matched to the scene's energy.

## Secondary motion

Settled elements should not be frozen corpses. Give the focal element a subtle idle: a 1–2° rotation sway, a ±6px float, a slow gradient shift — driven by `Math.sin(frame / fps * speed)`, small enough that captions stay readable. One idle motion per scene; never animate everything.

## Contrast on dark backgrounds

- The background is one uniform dark layer (`palette.bg`); content must pop off it: light text (`palette.paper` / `ink`-inverted), one saturated accent (`palette.accent`) reserved for THE key element, `accentSoft` for secondary fills.
- Never place mid-grey text on dark grey — keep text/background contrast obviously high (aim >= 7:1).
- Soft glows behind focal objects are fine only as full-canvas SVG/radial layers (no clipped glow boxes), and gentle — the talking-head video around the slot is bright and busy, so the scene reads calmer than you think.
