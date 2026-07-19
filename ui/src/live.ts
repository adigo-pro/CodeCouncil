/**
 * Liveness machinery: reveal-pacing for the activity feed, pop-up toasts for
 * verdicts/grades/rewrites, and the beat countdown. All derived from real
 * payload diffs between polls — nothing synthetic.
 */
import { useEffect, useRef, useState } from "react";
import type { ActivityEvent, Council } from "./types";

const REVEAL_MS = 420;
const TOAST_TTL_MS = 8000;
const BEAT_INTERVAL_S = 30;

/**
 * Events arrive in 30s observer batches; reveal unseen ones one at a time so
 * the feed streams instead of jumping. The initial page load reveals history
 * instantly — only genuinely new events get paced. Returns the visible slice
 * plus the seq watermark at first load (rows above it are "fresh" and animate).
 */
export function useReveal(activity: ActivityEvent[]): { visible: ActivityEvent[]; freshAfter: number } {
  const [cutoff, setCutoff] = useState<number | null>(null);
  const freshAfter = useRef<number>(Infinity);
  const maxSeq = activity.length ? activity[activity.length - 1].seq : -1;

  useEffect(() => {
    if (cutoff === null) {
      if (activity.length) {
        freshAfter.current = maxSeq;
        setCutoff(maxSeq);
      }
      return;
    }
    const next = activity.find((e) => e.seq > cutoff);
    if (next === undefined) return;
    const t = window.setTimeout(() => setCutoff(next.seq), REVEAL_MS);
    return () => window.clearTimeout(t);
  }, [activity, cutoff, maxSeq]);

  return {
    visible: cutoff === null ? [] : activity.filter((e) => e.seq <= cutoff),
    freshAfter: freshAfter.current,
  };
}

export interface Toast {
  id: string;
  kind: "pass" | "suggestion" | "error" | "grade" | "rewrite";
  title: string;
  body: string;
  born: number;
}

/** Diffs consecutive payloads into pop-up toasts. First load never toasts. */
export function useToasts(data: Council | null): Toast[] {
  const prev = useRef<Council | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    if (!data) return;
    const p = prev.current;
    prev.current = data;
    if (!p) return;

    const fresh: Toast[] = [];
    const born = Date.now();

    const known = new Set(p.verdicts.map((v) => v.id));
    for (const v of data.verdicts) {
      if (known.has(v.id)) continue;
      if (v.verdict === "PASS") {
        fresh.push({
          id: `v-${v.id}`, kind: "pass", born,
          title: `Critic · PASS`,
          body: `beat ${v.beat} — reviewed ${v.nEvents} events, nothing worth an interruption`,
        });
      } else if (v.verdict === "SUGGESTION") {
        const r = data.reviews.find((x) => x.id === v.id);
        fresh.push({
          id: `v-${v.id}`, kind: "suggestion", born,
          title: `Critic flagged an issue · ${r?.severity ?? "medium"}`,
          body: r ? `${r.file}${r.line ? `:${r.line}` : ""} — ${r.issue}` : `beat ${v.beat}`,
        });
      } else {
        fresh.push({
          id: `v-${v.id}`, kind: "error", born,
          title: "Critic · beat errored",
          body: v.error ?? "",
        });
      }
    }

    const prevOutcome = new Map(p.reviews.map((r) => [r.id, r.outcome]));
    for (const r of data.reviews) {
      const before = prevOutcome.get(r.id);
      if (before !== undefined && before !== r.outcome && r.outcome) {
        fresh.push({
          id: `g-${r.id}-${r.outcome}`, kind: "grade", born,
          title: `Reflector graded · ${r.outcome}`,
          body: r.issue,
        });
      }
    }

    if (data.heuristics.version > p.heuristics.version) {
      fresh.push({
        id: `h-${data.heuristics.version}`, kind: "rewrite", born,
        title: `Heuristics rewritten · v${p.heuristics.version} → v${data.heuristics.version}`,
        body: "The Reflector rewrote the Critic's review rules from graded outcomes.",
      });
    }

    if (fresh.length) setToasts((t) => [...t, ...fresh].slice(-4));
  }, [data]);

  // expire
  useEffect(() => {
    const i = window.setInterval(
      () => setToasts((t) => t.filter((x) => Date.now() - x.born < TOAST_TTL_MS)),
      500,
    );
    return () => window.clearInterval(i);
  }, []);

  return toasts;
}

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
