import type { Council, Review, Verdict } from "../types";
import { ago } from "../useCouncil";

const SEVERITY: Record<string, string> = {
  high: "border-bad/40 bg-bad/5 text-bad",
  medium: "border-warn/40 bg-warn/5 text-warn",
  low: "border-border bg-muted text-muted-foreground",
};

const OUTCOME: Record<string, { label: string; cls: string }> = {
  accepted: { label: "accepted", cls: "bg-ok/10 text-ok" },
  rebutted: { label: "rebutted", cls: "bg-warn/10 text-warn" },
  ignored: { label: "ignored", cls: "bg-muted text-muted-foreground" },
  undelivered: { label: "undelivered", cls: "bg-muted text-muted-foreground/70" },
};

/** Dot-per-beat timeline of critic verdicts: hollow = PASS, filled = flagged. */
function VerdictStrip({ verdicts }: { verdicts: Verdict[] }) {
  const recent = verdicts.slice(-48);
  if (!recent.length) return null;
  return (
    <div className="flex items-center gap-[5px]" title="Recent critic beats">
      {recent.map((v) => (
        <span
          key={v.id}
          title={`beat ${v.beat} · ${v.verdict}${v.reason ? `: ${v.reason}` : ""}${v.error ? ` — ${v.error}` : ""}`}
          className={
            v.verdict === "PASS"
              ? "h-2 w-2 rounded-full border border-foreground/25"
              : v.verdict === "SUGGESTION"
                ? "h-2 w-2 rounded-full bg-foreground"
                : "h-2 w-2 rounded-full bg-bad/60"
          }
        />
      ))}
    </div>
  );
}

function ReviewCard({ r, now }: { r: Review; now?: string }) {
  const outcome = r.outcome ? OUTCOME[r.outcome] : null;
  const ageS = now ? (new Date(now).getTime() - new Date(r.ts).getTime()) / 1000 : Infinity;
  const isNew = ageS < 90;
  return (
    <div
      className={`row-in rounded-2xl border bg-background p-5 transition-colors duration-1000 ${
        isNew ? "border-foreground/35 shadow-sm" : "border-border"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${SEVERITY[r.severity] ?? SEVERITY.low}`}>
          {r.severity}
        </span>
        <span className="font-mono text-xs text-muted-foreground">
          {r.file}
          {r.line ? `:${r.line}` : ""}
        </span>
        <span className="ml-auto flex items-center gap-2">
          {outcome ? (
            <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${outcome.cls}`}>
              {outcome.label}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border px-2.5 py-0.5 text-[11px] text-muted-foreground">
              <span className="live-dot h-1 w-1 rounded-full bg-muted-foreground" />
              awaiting grade
            </span>
          )}
          <span className="font-mono text-[11px] text-muted-foreground">{ago(r.ts, now)} ago</span>
        </span>
      </div>
      <p className="mt-3 text-[15px] leading-relaxed">{r.issue}</p>
      {r.rationale && (
        <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">{r.rationale}</p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <span className="font-mono">beat {r.beat}</span>
        <span>·</span>
        <span className="font-mono">heuristics v{r.heuristicsVersion}</span>
        {r.deliveredVia.map((c) => (
          <span key={c} className="rounded-full bg-muted px-2 py-0.5 font-mono">
            → {c === "context" ? "injected in context" : "blocked stop"}
          </span>
        ))}
        {r.fileTouched !== null && (
          <span className="font-mono">
            {r.fileTouched ? "✓ file touched after delivery" : "file untouched after delivery"}
          </span>
        )}
      </div>
      {r.evidence && (
        <p className="mt-3 border-t border-border pt-3 text-[13px] italic text-muted-foreground">
          reflector: {r.evidence}
        </p>
      )}
    </div>
  );
}

export function ReviewPanel({ data }: { data: Council | null }) {
  const reviews = data?.reviews ?? [];
  const s = data?.stats;
  return (
    <div className="flex h-full flex-col rounded-3xl border border-border bg-card p-6 md:p-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-lg font-medium">Peer review</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {s
              ? `${s.passes} quiet passes · ${s.suggestions} interruptions worth making`
              : "Every verdict the Critic has issued."}
            {data?.live.lastVerdictTs && (
              <span className="ml-2 font-mono text-xs">
                · last verdict {ago(data.live.lastVerdictTs, data.now)} ago
              </span>
            )}
          </p>
        </div>
        <VerdictStrip verdicts={data?.verdicts ?? []} />
      </div>

      <div className="mt-6 flex-1 space-y-3 overflow-y-auto" style={{ maxHeight: 560 }}>
        {reviews.length ? (
          reviews.map((r) => <ReviewCard key={r.id} r={r} now={data?.now} />)
        ) : (
          <div className="flex h-48 flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-border">
            <p className="text-sm font-medium">Nothing worth interrupting for — yet.</p>
            <p className="max-w-sm text-center text-xs text-muted-foreground">
              The Critic mostly says PASS by design. When it flags an issue, it lands here —
              and in the coding agent's own context via hooks.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
