import type { Council } from "../types";
import { ago } from "../useCouncil";

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

      <ul className="mt-6 max-h-80 space-y-3 overflow-y-auto text-sm leading-relaxed">
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

      {h && h.evolution.length > 0 && (
        <div className="mt-6 border-t border-border pt-4">
          <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
            What changed in v{h.evolution[h.evolution.length - 1].version}
          </p>
          <ul className="mt-3 space-y-2 text-[13px] leading-relaxed">
            {h.evolution[h.evolution.length - 1].added.map((r, i) => (
              <li key={`a${i}`} className="flex gap-2.5">
                <span className="mt-px shrink-0 font-mono text-ok">+</span>
                <span>{r}</span>
              </li>
            ))}
            {h.evolution[h.evolution.length - 1].removed.map((r, i) => (
              <li key={`r${i}`} className="flex gap-2.5 text-muted-foreground">
                <span className="mt-px shrink-0 font-mono text-bad/70">−</span>
                <span className="line-through decoration-border">{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {h && h.rewrites.length > 0 && (
        <div className="mt-4 border-t border-border pt-4">
          <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
            Rewrite history
          </p>
          <ul className="mt-3 space-y-2.5">
            {h.rewrites.slice(0, 4).map((r, i) => (
              <li key={`${r.from}-${r.to}-${r.ts}-${i}`} className="text-[13px] leading-snug">
                <span className="font-mono text-xs text-muted-foreground">
                  v{r.from}→v{r.to} · {ago(r.ts, data?.now)} ago ·{" "}
                  {["accepted", "rebutted", "ignored"]
                    .filter((g) => r.stats[g])
                    .map((g) => `${r.stats[g]} ${g}`)
                    .join(", ") || "no grades"}
                </span>
                <p className="mt-0.5 text-muted-foreground">{r.headline}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {h && (
        <div className={`${h.evolution.length || h.rewrites.length ? "mt-4" : "mt-6 border-t border-border"} pt-4 text-xs text-muted-foreground`}>
          <span className="font-mono">
            {h.historyCount
              ? `${h.historyCount} prior version${h.historyCount === 1 ? "" : "s"} archived`
              : "no rewrites yet — grades accumulate first"}
          </span>
        </div>
      )}

      {data && data.receipts.length > 0 && (
        <div className="mt-4 border-t border-border pt-4">
          <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
            Session receipts
          </p>
          <ul className="mt-3 space-y-1.5">
            {data.receipts.map((r) => (
              <li key={r.name}>
                <a
                  href={`/api/receipt/${encodeURIComponent(r.name)}`}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono text-[11px] text-muted-foreground underline decoration-border underline-offset-4 hover:text-foreground"
                >
                  {r.name}
                </a>
                <span className="ml-2 font-mono text-[11px] text-muted-foreground/70">
                  {ago(r.mtime, data.now)} ago
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
