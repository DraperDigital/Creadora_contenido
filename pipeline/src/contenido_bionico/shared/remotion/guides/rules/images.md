---
name: images
description: Sizing and positioning bundled image assets with Img
metadata:
  tags: images, Img, staticFile, assets
---

# Images in this pipeline

The ONLY image reference allowed is `staticFile("assets/<id>.png")` pointing at a REAL bundled asset, rendered with `<Img>` (both named imports from `remotion`). Never a remote URL, never a path outside `assets/`. When no suitable bundled asset exists, build the visual programmatically (inline SVG / CSS / React) instead.

## Sizing and positioning

Use the `style` prop to control size and position on the 1080x1920 canvas:

```tsx
import { Img, staticFile } from "remotion";

<Img
  src={staticFile("assets/logo.png")}
  style={{
    width: 500,
    height: 300,
    position: "absolute",
    top: 700,
    left: 290, // centered: left = (1080 - width) / 2
    objectFit: "cover",
  }}
/>
```

- Keep the focal image centered on x = 540 and settled above y = 1536 (platform safe zone).
- Size explicitly — never rely on the image's intrinsic dimensions.
- Animate the wrapper (`opacity`, `transform`) from `useCurrentFrame()` like any other element; images obey the same exit envelope.

## Dynamic asset paths

Template literals work for choosing among real bundled assets:

```tsx
<Img src={staticFile(`assets/${isActive ? "icon-on" : "icon-off"}.png`)} />
```

Every path the expression can produce must exist in the bundle — a missing asset fails the render.
