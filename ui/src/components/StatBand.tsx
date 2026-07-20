import type { Council } from "../types";

/** Slim strip of the numbers that actually mean something. */
export function StatBand({ data }: { data: Council | null }) {
  const s = data?.stats;
  const stats: { v: string; l: string }[] = [
    { v: s ? String(s.verdicts) : "—", l: "verdicts" },
    { v: s ? String(s.passes) : "—", l: "quiet passes" },
    { v: s ? String(s.suggestions) : "—", l: "issues flagged" },
    {
      v: s && s.acceptanceRate !== null ? `${Math.round(s.acceptanceRate * 100)}%` : "—",
      l: s?.graded ? `acceptance (n=${s.graded})` : "acceptance",
    },
  ];
  return (
    <section className="mt-8 flex flex-wrap items-baseline gap-x-10 gap-y-4 border-b border-border pb-6">
      {stats.map((x) => (
        <div key={x.l} className="flex items-baseline gap-2.5">
          <span className="font-mono text-2xl font-medium tracking-tight">{x.v}</span>
          <span className="text-xs text-muted-foreground">{x.l}</span>
        </div>
      ))}
    </section>
  );
}
