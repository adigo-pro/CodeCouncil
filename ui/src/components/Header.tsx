import type { Council } from "../types";
import { useBeatProgress } from "../live";

/** Three nodes, one raised — the council mark. */
function Mark() {
  return (
    <svg width="30" height="20" viewBox="0 0 30 20" fill="none" className="text-foreground">
      <circle cx="5" cy="14" r="3.2" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="15" cy="5" r="3.2" fill="currentColor" />
      <circle cx="25" cy="14" r="3.2" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export function Header({ data, connected }: { data: Council | null; connected: boolean }) {
  const live = connected && !!data?.live.observerLive;
  const beatFrac = useBeatProgress(data?.live.lastObservationTs ?? null);
  return (
    <header className="sticky top-0 z-50 w-full border-b border-border/60 bg-background/80 backdrop-blur-md">
      {/* countdown to the observer's next beat */}
      <div className="absolute inset-x-0 bottom-0 h-px bg-transparent">
        <div
          className="h-px bg-foreground/30 transition-[width] duration-150 ease-linear"
          style={{ width: live ? `${beatFrac * 100}%` : 0 }}
        />
      </div>
      <div className="mx-auto flex h-16 max-w-[1400px] items-center justify-between px-6">
        <div className="flex items-center gap-3">
          <Mark />
          <span className="text-[15px] font-semibold tracking-tight">CodeCouncil</span>
        </div>

        <div className="flex items-center gap-2.5">
          {data && (
            <>
              <span className="hidden rounded-full border border-border px-3 py-1.5 font-mono text-xs text-muted-foreground md:inline">
                watching <span className="text-foreground">{data.repo.name}</span>
                <span className="mx-2 text-border">·</span>
                beat <span className="text-foreground">{data.stats.beats}</span>
              </span>
              <span className="rounded-full border border-border px-3 py-1.5 font-mono text-xs">
                heuristics v{data.stats.heuristicsVersion}
              </span>
            </>
          )}
          {data && (
            <span
              className={`hidden items-center gap-2 rounded-full px-3 py-1.5 font-mono text-xs sm:inline-flex ${
                connected && data.live.criticLive
                  ? "bg-live/10 text-live"
                  : "border border-border text-muted-foreground"
              }`}
              title="Critic heartbeat"
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  connected && data.live.criticLive ? "live-dot bg-live" : "bg-muted-foreground/50"
                }`}
              />
              critic
            </span>
          )}
          <span
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium ${
              live
                ? "bg-live/10 text-live"
                : "border border-border text-muted-foreground"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${live ? "live-dot bg-live" : "bg-muted-foreground/50"}`}
            />
            {live ? "LIVE" : connected ? "IDLE" : "OFFLINE"}
          </span>
        </div>
      </div>
    </header>
  );
}
