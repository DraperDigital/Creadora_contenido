---
name: measuring-dom-nodes
description: Layout measurement without state — compute from the fixed canvas, DOM measurement as last resort
metadata:
  tags: measure, layout, dimensions, useLayoutEffect
---

# Measuring layout in this pipeline

`useState` and `useEffect` are forbidden (all motion and layout must be deterministic per frame), so the classic measure-then-set-state pattern is NOT available. Use these strategies instead, in order of preference:

## 1. Compute mathematically (preferred)

The canvas is always exactly 1080x1920 and the font is always `'Poppins'`. Derive sizes from known constants — column width = `1080 - 2 * margin`, bar height = `value / max * chartHeight`, etc. Fixed inputs mean you can lay out the whole scene with arithmetic; no measurement needed.

## 2. `fitText` for text (allowed)

For text that must fill or fit a width, use `fitText` from `@remotion/layout-utils` — see [measuring-text.md](measuring-text.md).

## 3. Last resort: `useRef` + `useLayoutEffect` with imperative style writes

When you genuinely must react to a DOM node's real size (rare), measure inside `useLayoutEffect` and write styles imperatively to the node — never into state:

```tsx
import React, { useRef, useLayoutEffect } from "react";

const AutoShrinkLabel: React.FC<{ children: string }> = ({ children }) => {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Shrink font until the content fits its box (never below the readable floor).
    let size = 72;
    el.style.fontSize = `${size}px`;
    while (el.scrollWidth > el.clientWidth && size > 34) {
      size -= 2;
      el.style.fontSize = `${size}px`;
    }
  });

  return (
    <div ref={ref} style={{ width: 900, whiteSpace: "nowrap", overflow: "hidden" }}>
      {children}
    </div>
  );
};
```

`useLayoutEffect` runs before the frame is captured, so the adjustment is rendered. Keep the logic deterministic — it must produce the same result on every frame for the same content.
