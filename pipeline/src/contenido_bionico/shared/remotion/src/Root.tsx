import React from "react";
import { Composition } from "remotion";
import { COMPOSITIONS } from "./Compositions.generated";
import { FPS } from "./lib/config";

export const Root: React.FC = () => (
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
