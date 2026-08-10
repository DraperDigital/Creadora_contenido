---
name: sequencing
description: Sequencing patterns for Remotion - delay, trim, limit duration of items
metadata:
  tags: sequence, timing, delay, trim
---

Use `<Sequence>` (named import from `remotion` — the only sequencing component on the allowed import surface) to delay when an element appears in the timeline.

```tsx
import { Sequence, AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig, Easing } from "remotion";

const Title = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const opacity = interpolate(frame, [0, 0.6 * fps], [0, 1], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return <div style={{ opacity }}>Title</div>;
};

const Main = () => {
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: "transparent" }}>
      <Sequence>
        <Background />
      </Sequence>
      <Sequence from={1 * fps} layout="none">
        <Title />
      </Sequence>
      <Sequence from={2 * fps} layout="none">
        <Subtitle />
      </Sequence>
    </AbsoluteFill>
  );
};
```

By default a `<Sequence>` wraps its children in an absolute-fill element. If the items should keep their own layout, use `layout="none"`.

Use `durationInFrames` to unmount the content after a given duration. In this pipeline prefer animating opacity to 0 (per the exit envelope) over hard unmounts mid-slot — an unmount is an instant disappearance.

## Sequential beats without `<Series>`

`<Series>` is NOT on the allowed import surface. To play beats one after another, stack `<Sequence>`s with explicit `from` offsets — in this pipeline, anchored to spoken words' start times from `props.words`:

```tsx
const beatA = Math.round((words.find((w) => w.word.includes("primero"))?.start ?? 1.0) * fps);
const beatB = Math.round((words.find((w) => w.word.includes("después"))?.start ?? 4.0) * fps);

<Sequence from={beatA} durationInFrames={beatB - beatA} layout="none">
  <BeatA />
</Sequence>
<Sequence from={beatB} layout="none">
  <BeatB />
</Sequence>
```

## Frame references inside sequences

Inside a Sequence, `useCurrentFrame()` returns the LOCAL frame (starting from 0):

```tsx
<Sequence from={60} durationInFrames={30}>
  <MyComponent />
  {/* Inside MyComponent, useCurrentFrame() returns 0-29, not 60-89 */}
</Sequence>
```

Remember this when mixing word-start anchors (absolute on the slot) with nested sequences: either keep all timing at the top level, or subtract the sequence's `from` when anchoring inside it.

## Nested sequences

Sequences can be nested for grouped timing:

```tsx
<Sequence from={0} durationInFrames={120}>
  <Background />
  <Sequence from={15} durationInFrames={90} layout="none">
    <Title />
  </Sequence>
  <Sequence from={45} durationInFrames={60} layout="none">
    <Subtitle />
  </Sequence>
</Sequence>
```
