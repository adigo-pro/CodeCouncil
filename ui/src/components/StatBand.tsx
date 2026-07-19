import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Council } from "../types";

/** Counts up/down to a new value over ~600ms when it changes. */
function AnimatedNumber({ value, suffix = "" }: { value: number; suffix?: string }) {
  const [display, setDisplay] = useState(value);
  const from = useRef(value);

  useEffect(() => {
    const start = from.current;
    from.current = value;
    if (start === value) return;
    const t0 = performance.now();
    let raf: number;
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / 600);
      const eased = 1 - Math.pow(1 - k, 3);
      setDisplay(Math.round(start + (value - start) * eased));
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value]);

  return (
    <>
      {display.toLocaleString("en-US")}
      {suffix}
    </>
  );
}

export function StatBand({ data }: { data: Council | null }) {
  const s = data?.stats;
  const stats: { v: ReactNode; l: string }[] = [
    { v: s ? <AnimatedNumber value={s.beats} /> : "—", l: "beats observed" },
    { v: s ? <AnimatedNumber value={s.events} /> : "—", l: "intent + diff events" },
    { v: s ? <AnimatedNumber value={s.verdicts} /> : "—", l: "critic verdicts" },
    { v: s ? <AnimatedNumber value={s.suggestions} /> : "—", l: "issues flagged" },
    {
      v:
        s && s.acceptanceRate !== null ? (
          <AnimatedNumber value={Math.round(s.acceptanceRate * 100)} suffix="%" />
        ) : (
          "—"
        ),
      l: s?.graded ? `acceptance · n=${s.graded}` : "acceptance rate",
    },
  ];
  return (
    <section className="mt-12 border-y border-border">
      <div className="grid grid-cols-2 gap-x-6 gap-y-10 py-10 md:grid-cols-5">
        {stats.map((x) => (
          <div key={x.l}>
            <div className="font-mono text-4xl font-medium tracking-tight md:text-5xl">{x.v}</div>
            <div className="mt-2 text-xs text-muted-foreground md:text-sm">{x.l}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
