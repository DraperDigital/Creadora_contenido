// AUTO-GENERATED per-render entrypoint. Transient — safe to delete.
import { registerRoot } from "remotion";
import React from "react";
import { Composition } from "remotion";
import { FPS } from "./lib/config";
import { COMPOSITIONS } from "./Compositions.fca9c27a88f5";

const Root: React.FC = () => (
  <>
    {COMPOSITIONS.map(({ id, component, durationInFrames, width, height, defaultProps }) => (
      <Composition
        key={id}
        id={id}
        component={component}
        durationInFrames={durationInFrames}
        fps={FPS}
        width={width}
        height={height}
        defaultProps={defaultProps as any}
      />
    ))}
  </>
);

registerRoot(Root);
