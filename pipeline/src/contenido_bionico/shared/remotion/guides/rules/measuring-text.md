---
name: measuring-text
description: Fitting text to a width with fitText
metadata:
  tags: measure, text, layout, fitText
---

# Fitting text in this pipeline

The only layout-utils API on the allowed import surface is `fitText` from `@remotion/layout-utils`. (`measureText` and `fillTextBox` are NOT allowed — if text might overflow, shorten it or use `fitText`.)

`'Poppins'` is already loaded globally by the render tree, so it is safe to measure against.

## Fitting text to a width

```tsx
import { fitText } from "@remotion/layout-utils";

const { fontSize } = fitText({
  text: "Titular grande",
  withinWidth: 900, // readable column on the 1080-wide canvas
  fontFamily: "Poppins",
  fontWeight: "900",
});

return (
  <div
    style={{
      fontSize: Math.min(fontSize, 220), // cap at the giant-hero ceiling
      fontFamily: "'Poppins', sans-serif",
      fontWeight: 900,
    }}
  >
    Titular grande
  </div>
);
```

## Best practices

- **Match font properties:** use the exact same `fontFamily`, `fontWeight`, and `letterSpacing` for the `fitText` call and the rendered element, or the measurement is wrong.
- **Respect the type-scale minima:** if `fitText` returns a size below the contract minimum (body 56px, headline 120px, readable floor ~34px), do not render smaller — shorten the text instead.
- **Cap the result:** always wrap in `Math.min(fontSize, cap)` so a two-character string does not explode to fill the width.
