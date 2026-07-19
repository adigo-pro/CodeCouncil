import type { Council, CurvePoint } from "../types";

const W = 720;
const H = 280;
const PAD = { top: 28, right: 24, bottom: 46, left: 44 };

const x = (i: number, n: number) => {
  const span = W - PAD.left - PAD.right;
  if (n === 1) return PAD.left + span / 2;
  return PAD.left + (span * i) / (n - 1);
};
const y = (v: number) => PAD.top + (1 - v) * (H - PAD.top - PAD.bottom);

function Empty() {
  const steps = ["observer", "critic", "hooks", "reflector"];
  return (
    <div className="flex h-[280px] flex-col items-center justify-center gap-6">
      <div className="flex items-center gap-3 font-mono text-sm text-muted-foreground">
        {steps.map((s, i) => (
          <span key={s} className="flex items-center gap-3">
            <span className="rounded-full border border-border px-3 py-1.5">{s}</span>
            {i < steps.length - 1 && <span className="text-border">→</span>}
          </span>
        ))}
      </div>
      <p className="max-w-md text-center text-sm text-muted-foreground">
        The curve appears once the Reflector grades delivered suggestions against what the
        developer actually did next. Every point is a real graded outcome.
      </p>
    </div>
  );
}

function Chart({ curve }: { curve: CurvePoint[] }) {
  const n = curve.length;
  const pts = curve
    .map((c, i) => ({ ...c, i }))
    .filter((c) => c.acceptance !== null) as (CurvePoint & { i: number })[];

  const line = pts.map((p, k) => `${k ? "L" : "M"}${x(p.i, n)},${y(p.acceptance!)}`).join(" ");
  const area =
    pts.length > 1
      ? `${line} L${x(pts[pts.length - 1].i, n)},${y(0)} L${x(pts[0].i, n)},${y(0)} Z`
      : "";
  const xpts = curve
    .map((c, i) => ({ ...c, i }))
    .filter((c) => c.xcheck !== null) as (CurvePoint & { i: number })[];
  const xline = xpts.map((p, k) => `${k ? "L" : "M"}${x(p.i, n)},${y(p.xcheck!)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full">
      {/* gridlines */}
      {[0, 0.25, 0.5, 0.75, 1].map((g) => (
        <g key={g}>
          <line
            x1={PAD.left}
            x2={W - PAD.right}
            y1={y(g)}
            y2={y(g)}
            stroke="var(--color-border)"
            strokeWidth={g === 0 ? 1.2 : 1}
            strokeDasharray={g === 0 ? undefined : "2 5"}
          />
          <text
            x={PAD.left - 10}
            y={y(g) + 3.5}
            textAnchor="end"
            fontSize="10.5"
            fill="var(--color-muted-foreground)"
            fontFamily="var(--font-mono)"
          >
            {Math.round(g * 100)}
          </text>
        </g>
      ))}

      {/* xcheck consistency, dashed */}
      {xpts.length > 1 && (
        <path d={xline} fill="none" stroke="var(--color-muted-foreground)" strokeWidth="1.3" strokeDasharray="4 5" opacity="0.55" />
      )}

      {/* acceptance area + line */}
      {area && <path d={area} fill="var(--color-foreground)" opacity="0.05" />}
      {pts.length > 1 && (
        <path d={line} fill="none" stroke="var(--color-foreground)" strokeWidth="2" strokeLinejoin="round" />
      )}

      {/* per-version marks */}
      {curve.map((c, i) => {
        const cx = x(i, n);
        const has = c.acceptance !== null;
        const cy = has ? y(c.acceptance!) : y(0);
        return (
          <g key={c.version}>
            {has ? (
              <>
                <circle cx={cx} cy={cy} r="4.5" fill="var(--color-foreground)" stroke="var(--color-background)" strokeWidth="2" />
                <text
                  x={cx}
                  y={cy - 12}
                  textAnchor="middle"
                  fontSize="12"
                  fontWeight="500"
                  fill="var(--color-foreground)"
                  fontFamily="var(--font-mono)"
                >
                  {Math.round(c.acceptance! * 100)}%
                </text>
              </>
            ) : (
              <circle cx={cx} cy={cy} r="4" fill="none" stroke="var(--color-muted-foreground)" strokeWidth="1.5" strokeDasharray="2 2" />
            )}
            <text
              x={cx}
              y={H - PAD.bottom + 20}
              textAnchor="middle"
              fontSize="11.5"
              fontWeight="500"
              fill="var(--color-foreground)"
              fontFamily="var(--font-mono)"
            >
              v{c.version}
            </text>
            <text
              x={cx}
              y={H - PAD.bottom + 34}
              textAnchor="middle"
              fontSize="10"
              fill="var(--color-muted-foreground)"
              fontFamily="var(--font-mono)"
            >
              n={c.graded}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** Segmented outcome bar for one heuristics version. */
function Breakdown({ c }: { c: CurvePoint }) {
  const segs = [
    { k: "accepted", v: c.accepted, cls: "bg-ok" },
    { k: "rebutted", v: c.rebutted, cls: "bg-warn" },
    { k: "ignored", v: c.ignored, cls: "bg-muted-foreground/40" },
    { k: "undelivered", v: c.undelivered, cls: "bg-border" },
  ].filter((s) => s.v > 0);
  const total = segs.reduce((a, s) => a + s.v, 0);
  if (!total) return null;
  return (
    <div className="flex items-center gap-4">
      <span className="w-8 shrink-0 font-mono text-xs text-muted-foreground">v{c.version}</span>
      <div className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        {segs.map((s) => (
          <div key={s.k} className={s.cls} style={{ width: `${(s.v / total) * 100}%` }} title={`${s.k}: ${s.v}`} />
        ))}
      </div>
      <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
        {segs.map((s) => `${s.v} ${s.k}`).join(" · ")}
      </span>
    </div>
  );
}

export function ImprovementChart({ data }: { data: Council | null }) {
  const curve = data?.curve ?? [];
  const anyGraded = curve.some((c) => c.graded > 0);
  const hasXcheck = curve.filter((c) => c.xcheck !== null).length > 1;

  return (
    <div className="flex h-full flex-col rounded-3xl border border-border bg-card p-6 md:p-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-medium">Self-improvement</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Suggestion acceptance per heuristics version — the same signal the Reflector
            rewrites the Critic on.
          </p>
        </div>
        {hasXcheck && (
          <span className="flex shrink-0 items-center gap-2 pt-1 text-xs text-muted-foreground">
            <svg width="22" height="4"><line x1="0" y1="2" x2="22" y2="2" stroke="currentColor" strokeWidth="1.3" strokeDasharray="4 5" /></svg>
            grade ↔ code consistency
          </span>
        )}
      </div>

      <div className="mt-6 flex-1">
        {curve.length && anyGraded ? <Chart curve={curve} /> : <Empty />}
      </div>

      {anyGraded && (
        <div className="mt-6 space-y-2.5 border-t border-border pt-5">
          {curve.filter((c) => c.graded + c.undelivered > 0).map((c) => (
            <Breakdown key={c.version} c={c} />
          ))}
        </div>
      )}
    </div>
  );
}
