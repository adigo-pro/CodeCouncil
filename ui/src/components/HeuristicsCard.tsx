import type { Council } from "../types";

export function HeuristicsCard({ data }: { data: Council | null }) {
  const h = data?.heuristics;
  return (
    <div className="flex h-full flex-col rounded-3xl border border-border bg-card p-6 md:p-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-medium">The Critic's brain</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Review heuristics, rewritten by the Reflector from graded outcomes.
          </p>
        </div>
        {h && (
          <span className="shrink-0 rounded-full bg-primary px-3 py-1.5 font-mono text-xs text-primary-foreground">
            v{h.version}
          </span>
        )}
      </div>

      <ul className="mt-6 flex-1 space-y-3 overflow-y-auto text-sm leading-relaxed" style={{ maxHeight: 460 }}>
        {(h?.rules ?? []).map((r, i) => (
          <li key={i} className="flex gap-3">
            <span className="mt-[9px] h-1 w-1 shrink-0 rounded-full bg-foreground/60" />
            <span>{r}</span>
          </li>
        ))}
        {!h?.rules.length && (
          <li className="text-sm text-muted-foreground">No heuristics file yet — the Critic seeds v1 on its first beat.</li>
        )}
      </ul>

      {h && (
        <div className="mt-6 border-t border-border pt-4 text-xs text-muted-foreground">
          <span className="font-mono">
            {h.historyCount
              ? `${h.historyCount} prior version${h.historyCount === 1 ? "" : "s"} archived`
              : "no rewrites yet — grades accumulate first"}
          </span>
        </div>
      )}
    </div>
  );
}
