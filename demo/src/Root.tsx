import React from "react";
import { Composition } from "remotion";
import { Catch } from "./Catch";

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Catch"
    component={Catch}
    durationInFrames={780}
    fps={30}
    width={1280}
    height={720}
  />
);
