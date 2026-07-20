/** Beat countdown for the header — derived from real observation timestamps. */
import { useEffect, useState } from "react";

const BEAT_INTERVAL_S = 30;

/** 0..1 progress through the observer's beat window, ticking smoothly. */
export function useBeatProgress(
  lastObservationTs: string | null,
  intervalS: number = BEAT_INTERVAL_S,
): number {
  const [frac, setFrac] = useState(0);
  useEffect(() => {
    const i = window.setInterval(() => {
      if (!lastObservationTs) return setFrac(0);
      const el = (Date.now() - new Date(lastObservationTs).getTime()) / 1000;
      setFrac(Math.max(0, Math.min(1, el / Math.max(intervalS, 0.5))));
    }, 120);
    return () => window.clearInterval(i);
  }, [lastObservationTs, intervalS]);
  return frac;
}
