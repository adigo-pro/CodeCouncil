export interface CurvePoint {
  version: number;
  suggested: number;
  delivered: number;
  accepted: number;
  rebutted: number;
  ignored: number;
  undelivered: number;
  graded: number;
  acceptance: number | null;
  xcheck: number | null;
  xcheckN: number;
}

export interface Review {
  id: string;
  ts: string;
  beat: number;
  heuristicsVersion: number;
  file: string;
  line: number | null;
  issue: string;
  rationale: string;
  severity: "low" | "medium" | "high";
  deliveredVia: string[];
  outcome: "accepted" | "rebutted" | "ignored" | "undelivered" | null;
  evidence: string;
  fileTouched: boolean | null;
}

export interface Verdict {
  seq: number;
  id: string;
  ts: string;
  beat: number;
  verdict: "PASS" | "SUGGESTION" | "ERROR";
  heuristicsVersion: number;
  nEvents: number;
  severity: string | null;
  error: string | null;
}

export interface ActivityEvent {
  seq: number;
  ts: string;
  beat: number;
  kind: string; // reasoning | tool | diff
  summary: string;
  detail: string;
}

export interface Council {
  now: string;
  repo: { path: string; name: string };
  live: {
    lastObservationTs: string | null;
    lastVerdictTs: string | null;
    observerIntervalS: number;
    observerLive: boolean;
    criticLive: boolean;
  };
  stats: {
    beats: number;
    events: number;
    verdicts: number;
    passes: number;
    suggestions: number;
    graded: number;
    accepted: number;
    acceptanceRate: number | null;
    heuristicsVersion: number;
  };
  curve: CurvePoint[];
  reviews: Review[];
  verdicts: Verdict[];
  activity: ActivityEvent[];
  heuristics: {
    version: number;
    rules: string[];
    historyCount: number;
    evolution: { version: number; added: string[]; removed: string[] }[];
  };
}
