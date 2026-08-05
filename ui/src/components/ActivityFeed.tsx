import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ActivityEvent, Council } from "../types";
import { ago } from "../useCouncil";

function Row({ e }: { e: ActivityEvent }) {
  const [expanded, setExpanded] = useState(false);
  if (e.kind === "reasoning") {
    const expandable = !!e.full;
    const text = expanded && e.full ? e.full : e.summary;
    return (
      <p
        className={`text-neutral-400 ${expandable ? "cursor-pointer hover:text-neutral-300" : ""}`}
        title={expandable ? (expanded ? "collapse" : "show full thought") : undefined}
        onClick={() => expandable && setExpanded(!expanded)}
      >
        <span className="mr-2 text-purple-300/90">◆</span>
        <span className="italic">{text}</span>
        {expandable && (
          <span className="ml-1.5 text-neutral-600">{expanded ? "▴" : "▾"}</span>
        )}
      </p>
    );
  }
  if (e.kind === "tool") {
    return (
      <p className="text-neutral-300">
        <span className="mr-2 text-cyan-400/90">▸</span>
        <span className="text-cyan-300/90">{e.summary}</span>
        {e.detail && <span className="ml-2 text-neutral-500">{e.detail}</span>}
      </p>
    );
  }
  if (e.kind === "commit") {
    return (
      <p className="text-neutral-300">
        <span className="mr-2 text-amber-300/90">⎘</span>
        <span className="text-amber-200/80">committed: {e.summary}</span>
      </p>
    );
  }
  if (e.kind === "diff") {
    return (
      <p className="text-neutral-300">
        <span className="mr-2 text-green-400/90">±</span>
        <span className="text-green-300/80">{e.summary}</span>
      </p>
    );
  }
  return null;
}

export function ActivityFeed({ data }: { data: Council | null }) {
  const activity = data?.activity ?? [];
  const scroller = useRef<HTMLDivElement>(null);
  // Depend on the NEWEST event's stable id, not activity.length — the server
  // caps the feed at 120, so the length pins at 120 minutes into any session
  // and the effect would never fire again (auto-scroll dies exactly when a
  // session gets going). The last seq changes with every new event.
  const lastSeq = activity.length ? activity[activity.length - 1].seq : null;

  // Follow the tail unless the user has scrolled up to read.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [lastSeq]);

  const rows: ReactNode[] = [];
  let lastBeat: number | null = null;
  activity.forEach((e) => {
    if (e.beat !== lastBeat) {
      lastBeat = e.beat;
      rows.push(
        <div key={`b${e.beat}-${e.seq}`} className="mb-1.5 mt-4 flex items-center gap-3 first:mt-0">
          <span className="text-[10px] uppercase tracking-widest text-neutral-600">
            beat {e.beat}
          </span>
          <span className="h-px flex-1 bg-white/[0.07]" />
          <span className="text-[10px] text-neutral-600">{ago(e.ts, data?.now)} ago</span>
        </div>,
      );
    }
    rows.push(<Row key={e.seq} e={e} />);
  });

  const live = !!data?.live.observerLive;

  return (
    <div className="flex h-full min-h-[420px] flex-col overflow-hidden rounded-3xl bg-ink text-neutral-100">
      <div className="flex items-center justify-between border-b border-white/[0.08] px-5 py-3.5">
        <div className="flex items-center gap-2.5">
          <span className={`h-1.5 w-1.5 rounded-full ${live ? "live-dot bg-live" : "bg-neutral-600"}`} />
          <span className="font-mono text-xs text-neutral-400">
            observer — what the coding agent is doing
          </span>
        </div>
        <span className="font-mono text-[11px] text-neutral-500">
          {data?.stats.events ?? 0} events
        </span>
      </div>
      <div
        ref={scroller}
        className="min-h-0 flex-1 space-y-1.5 overflow-y-auto scroll-fade p-5 font-mono text-[11.5px] leading-relaxed"
      >
        {rows.length ? (
          rows
        ) : (
          <p className="text-neutral-500">Waiting for the observer's first beat…</p>
        )}
      </div>
    </div>
  );
}
