import { useEffect, useRef, useState } from "react";
import type { Council } from "./types";

const POLL_MS = 2000;

/** Polls the live aggregation endpoint. Keeps last good payload on errors. */
export function useCouncil(): { data: Council | null; connected: boolean } {
  const [data, setData] = useState<Council | null>(null);
  const [connected, setConnected] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetch("/api/council");
        if (!res.ok) throw new Error(String(res.status));
        const json = (await res.json()) as Council;
        if (alive) {
          setData(json);
          setConnected(true);
        }
      } catch {
        if (alive) setConnected(false);
      }
      if (alive) timer.current = window.setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer.current);
    };
  }, []);

  return { data, connected };
}

/** "12s", "3m", "2h" — compact relative time for feed rows. */
export function ago(ts: string | null, now?: string): string {
  if (!ts) return "—";
  const t = new Date(ts).getTime();
  const ref = now ? new Date(now).getTime() : Date.now();
  const s = Math.max(0, Math.round((ref - t) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}
