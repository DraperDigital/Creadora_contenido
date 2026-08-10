---
name: transitions
description: Beat-to-beat transitions inside one scene, built from interpolate — no extra packages
metadata:
  tags: transitions, crossfade, wipe, scale, radial, beats
---

`@remotion/transitions` is NOT installed. Build every transition from `interpolate()` + CSS on the elements you already have. A multi-beat scene switches between beats with one of these patterns; pick the one matching the scene's energy.

Compute one progress value per cut and drive both sides from it:

```tsx
const t = interpolate(frame, [cutF, cutF + 12], [0, 1], {
  easing: Easing.bezier(0.83, 0, 0.17, 1), // whip-pan
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
});
```

## Crossfade (calm)

```tsx
<div style={{ opacity: 1 - t }}>{beatA}</div>
<div style={{ opacity: t }}>{beatB}</div>
```

## Wipe (directional/process)

Reveal the incoming beat with a moving `clipPath`; optionally push the outgoing beat slightly the same way:

```tsx
<div style={{ transform: `translateX(${t * -60}px)`, opacity: 1 - t }}>{beatA}</div>
<div style={{ clipPath: `inset(0 ${(1 - t) * 100}% 0 0)` }}>{beatB}</div>
```

Vertical wipe: `inset(${(1 - t) * 100}% 0 0 0)`.

## Scale-through (punchy)

Outgoing beat scales up and fades; incoming beat scales from slightly small into place:

```tsx
<div style={{ transform: `scale(${1 + t * 0.15})`, opacity: 1 - t }}>{beatA}</div>
<div style={{ transform: `scale(${0.85 + t * 0.15})`, opacity: t }}>{beatB}</div>
```

## Radial reveal (focal burst)

```tsx
<div style={{ clipPath: `circle(${t * 120}% at 540px 800px)` }}>{beatB}</div>
```

Center the circle on the focal point of the incoming beat (on the x=540 axis).

## Push / slide

Both beats travel together like a filmstrip:

```tsx
<div style={{ transform: `translateX(${t * -1080}px)` }}>{beatA}</div>
<div style={{ transform: `translateX(${(1 - t) * 1080}px)` }}>{beatB}</div>
```

## Rules of thumb

- Anchor `cutF` to a spoken word's start from `props.words` (`Math.round(word.start * fps)`), not to an arbitrary second.
- 10–14 frames for punchy cuts, 18–24 for calm crossfades.
- One transition style per scene — do not mix a wipe, a radial, and a scale in the same slot.
- The full-bleed background NEVER transitions — it stays up while beats swap on top of it.
