---
name: timing
description: Intent-labeled motion library — named Bézier curves and spring presets, with when-to-use guidance
metadata:
  tags: easing, bezier, interpolation, spring, timing, motion
---

Drive motion with `interpolate()` over an explicit frame range, ALWAYS clamped both sides:

```ts
import { interpolate, Easing } from "remotion";

const v = interpolate(frame, [startF, startF + 18], [0, 1], {
  easing: Easing.bezier(0.16, 1, 0.3, 1),
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
});
```

Pick the curve by INTENT — do not reuse one bezier for every element. A scene reads as designed when entrances, emphasis, and exits each move differently.

## Named Bézier curves (copy-paste, pick by intent)

| Name | Curve | Use for |
| --- | --- | --- |
| `snappy-mechanical` | `Easing.bezier(0.16, 1, 0.3, 1)` | Crisp UI entrances: cards, labels, panels decelerating into place. ~15–20 frames. |
| `heavy-settle` | `Easing.bezier(0.22, 1, 0.36, 1)` | Big/heavy objects landing: hero numbers, large shapes, device mockups. ~20–28 frames. |
| `gentle-drift` | `Easing.bezier(0.45, 0, 0.55, 1)` | Slow editorial moves: background parallax, long pans, ambient float. 40+ frames. |
| `dramatic-slow-reveal` | `Easing.bezier(0.7, 0, 0.3, 1)` | One deliberate emphasis move: a bar filling to its final value, a line drawing itself. 30–60 frames. |
| `whip-pan` | `Easing.bezier(0.83, 0, 0.17, 1)` | Fast directional swaps: panel slides, split-screen switches, wipes. 10–14 frames. |
| `playful-overshoot` | `Easing.bezier(0.34, 1.56, 0.64, 1)` | Small pops that go slightly past the target and settle. Use sparingly, one or two per scene. ~12–18 frames. |
| `anticipate-exit` | `Easing.bezier(0.7, 0, 0.84, 0)` | EXITS: element accelerates away with gravity. Never use an ease-out for an exit. ~10–15 frames. |
| `linear-meter` | `Easing.linear` (default) | ONLY mechanical progress: counters, progress bars, clock hands. Wrong for anything organic. |

Direction rule: entrances decelerate (`snappy-mechanical`, `heavy-settle`), exits accelerate (`anticipate-exit`). Elements arrive with momentum and leave with gravity.

## Spring presets (prefer springs for pops, icon entrances, and emphasis)

`spring()` gives physical, alive motion that a bezier cannot fake. Pops, icon/badge entrances, checkmarks, number bumps, and emphasis hits should usually be springs, not beziers.

```ts
import { spring } from "remotion";

const p = spring({ frame: frame - startF, fps, config: { damping: 200 } });
```

| Name | Config | Use for |
| --- | --- | --- |
| `spring-snappy` | `{ damping: 200 }` | No bounce, fast confident settle (~0.5s). Default entrance spring for icons, badges, cards. |
| `spring-bouncy` | `{ damping: 12, stiffness: 170 }` | Visible playful bounce. Emphasis hits, mascot-ish pops, one hero moment per scene. Add `overshootClamping: true` if it must never exceed 1. |
| `spring-soft` | `{ damping: 18, stiffness: 80 }` | Organic ease with a hint of life. Bars/charts growing, staggered list items. |

Drive scale/translate from the spring value: `transform: \`scale(${p})\`` or `translateY(${(1 - p) * 60}px)`. Stagger siblings by offsetting the frame: `frame: frame - startF - i * 4`.

## Composing interpolations

When multiple properties share one timing, compute a single normalized progress and map each property from it — separate TIMING (when/how fast) from MAPPING (what values):

```tsx
const slideIn = interpolate(frame, [inStart, inStart + 18], [0, 1], {
  easing: Easing.bezier(0.16, 1, 0.3, 1),
  extrapolateLeft: "clamp", extrapolateRight: "clamp",
});
const slideOut = interpolate(frame, [outStart, outStart + 12], [0, 1], {
  easing: Easing.bezier(0.7, 0, 0.84, 0),
  extrapolateLeft: "clamp", extrapolateRight: "clamp",
});
const progress = slideIn - slideOut;

const x = interpolate(progress, [0, 1], [100, 0]);
const opacity = interpolate(progress, [0, 1], [0, 1]);
```
