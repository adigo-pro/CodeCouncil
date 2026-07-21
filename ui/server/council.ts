/**
 * Reads the live `.codecouncil/` files of a watched repo and aggregates them
 * into one JSON payload for the dashboard. Mirrors reflector/report.py's
 * acceptance-per-heuristics-version math so the curve on screen is the same
 * signal the Reflector acts on — no separate, fakeable metric.
 */
import fs from "node:fs";
import path from "node:path";

const GRADED = new Set(["accepted", "rebutted", "ignored"]);

// ---------- raw file readers ----------

function readNdjson(file: string): Record<string, any>[] {
  let text: string;
  try {
    text = fs.readFileSync(file, "utf-8");
  } catch {
    return [];
  }
  const rows: Record<string, any>[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      rows.push(JSON.parse(line));
    } catch {
      /* mid-write partial line — skip */
    }
  }
  return rows;
}

// Bounded reader for the append-only observations log, which grows without
// limit over a session. The dashboard only ever needs the tail (last ~120
// events, the latest beat, the newest timestamp), so parsing the whole file
// every 2s poll is wasted work. Reads at most maxBytes from the end and drops
// the partial first line the offset may land inside.
function readNdjsonTail(file: string, maxBytes = 1_000_000): Record<string, any>[] {
  let fd: number | undefined;
  try {
    const size = fs.statSync(file).size;
    const start = Math.max(0, size - maxBytes);
    const length = size - start;
    const buf = Buffer.allocUnsafe(length);
    fd = fs.openSync(file, "r");
    fs.readSync(fd, buf, 0, length, start);
    let text = buf.toString("utf-8");
    if (start > 0) text = text.slice(text.indexOf("\n") + 1); // drop partial line
    const rows: Record<string, any>[] = [];
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      try {
        rows.push(JSON.parse(line));
      } catch {
        /* mid-write partial line — skip */
      }
    }
    return rows;
  } catch {
    return [];
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function readJson(file: string): Record<string, any> {
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf-8"));
    return typeof raw === "object" && raw !== null ? raw : {};
  } catch {
    return {};
  }
}

function readText(file: string): string {
  try {
    return fs.readFileSync(file, "utf-8");
  } catch {
    return "";
  }
}

function mtime(file: string): number | null {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return null;
  }
}

// ---------- aggregation ----------

/** Port of reflector/report.py consistent(): model grade vs file_touched signal. */
function consistent(o: Record<string, any>): boolean | null {
  const touched = o.file_touched;
  if (touched === null || touched === undefined || !GRADED.has(o.outcome)) return null;
  if (o.outcome === "accepted") return !!touched;
  if (o.outcome === "ignored") return !touched;
  return true; // rebutted happens in words; either way is consistent
}

/** Port of reflector/report.py build_rows(): the improvement curve. */
function buildCurve(suggestions: Record<string, any>[], outcomes: Record<string, any>[]) {
  const per = new Map<number, Record<string, number>>();
  const row = (v: number) => {
    if (!per.has(v))
      per.set(v, {
        suggested: 0, delivered: 0, accepted: 0, rebutted: 0,
        ignored: 0, undelivered: 0, missed: 0, xcheckOk: 0, xcheckN: 0,
      });
    return per.get(v)!;
  };
  for (const s of suggestions) {
    if (s.verdict === "SUGGESTION") row(s.heuristics_version ?? 0).suggested += 1;
  }
  for (const o of outcomes) {
    const v = row(o.heuristics_version ?? 0);
    if (GRADED.has(o.outcome)) {
      v[o.outcome] += 1;
      v.delivered += 1;
      const ok = consistent(o);
      if (ok !== null) {
        v.xcheckN += 1;
        if (ok) v.xcheckOk += 1;
      }
    } else if (o.outcome === "undelivered") {
      v.undelivered += 1;
    } else if (o.outcome === "missed") {
      // Mirrors reflector/report.py build_rows: "missed" (a PASS later
      // contradicted by a fix commit, reflector.misses) is its own column,
      // deliberately excluded from the acceptance-rate denominator below —
      // the two measurement signals must stay independent (CLAUDE.md's
      // two-signal separation rule).
      v.missed += 1;
    }
  }
  return [...per.keys()].sort((a, b) => a - b).map((version) => {
    const v = per.get(version)!;
    const graded = v.accepted + v.rebutted + v.ignored;
    return {
      version,
      ...v,
      graded,
      acceptance: graded ? v.accepted / graded : null,
      xcheck: v.xcheckN ? v.xcheckOk / v.xcheckN : null,
    };
  });
}

function truncate(s: string, n: number): string {
  s = s.replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** One-line human summary per observation event, for the live feed. */
function summarizeEvent(
  e: Record<string, any>,
): { kind: string; summary: string; detail: string; full?: string } {
  const p = e.payload ?? {};
  if (e.type === "reasoning") {
    const text = String(p.text ?? "");
    const summary = truncate(text, 500);
    // full text rides along only when the summary actually cut something
    return { kind: "reasoning", summary, detail: "", full: summary.endsWith("…") ? text : "" };
  }
  if (e.type === "tool_call") {
    const inp = p.input ?? {};
    const detail = inp.file_path ?? inp.command ?? inp.pattern ?? inp.query ?? "";
    return { kind: "tool", summary: p.tool ?? "?", detail: truncate(String(detail), 160) };
  }
  if (e.type === "commit") {
    const subjects = (p.subjects ?? []) as string[];
    return { kind: "commit", summary: truncate(subjects.join("; "), 200), detail: "" };
  }
  if (e.type === "diff") {
    const stat = (p.stat ?? "").trim().split("\n").filter(Boolean);
    const last = stat.length ? stat[stat.length - 1].trim() : "";
    const untracked = (p.untracked ?? []).length;
    const bits = [last, untracked ? `${untracked} untracked` : ""].filter(Boolean);
    return { kind: "diff", summary: bits.join(" · ") || "no changes", detail: "" };
  }
  return { kind: e.type ?? "?", summary: "", detail: "" };
}

const heuristicsVersion = (text: string): number => {
  const m = text.match(/^version:\s*(\d+)/m);
  return m ? parseInt(m[1], 10) : 0;
};

/** Rule bullets from a heuristics file (top-level `- ` lines, joined continuations). */
function heuristicsRules(text: string): string[] {
  const rules: string[] = [];
  for (const line of text.split("\n")) {
    if (/^- /.test(line)) rules.push(line.slice(2).trim());
    else if (/^ {2,}\S/.test(line) && rules.length) rules[rules.length - 1] += " " + line.trim();
  }
  return rules;
}

/**
 * What the Reflector actually changed, version over version: parse every
 * archived heuristics file plus the current one, diff consecutive rule sets.
 * Exact-string diff — a rephrased rule shows as remove+add, which is honest.
 */
function heuristicsEvolution(historyDir: string, currentText: string) {
  const versions: { version: number; rules: string[] }[] = [];
  try {
    for (const f of fs.readdirSync(historyDir)) {
      if (!f.endsWith(".md")) continue;
      const text = readText(path.join(historyDir, f));
      versions.push({ version: heuristicsVersion(text), rules: heuristicsRules(text) });
    }
  } catch {
    /* no history yet */
  }
  if (currentText) {
    versions.push({ version: heuristicsVersion(currentText), rules: heuristicsRules(currentText) });
  }
  versions.sort((a, b) => a.version - b.version);
  const evolution = [];
  for (let i = 1; i < versions.length; i++) {
    const prev = new Set(versions[i - 1].rules);
    const next = new Set(versions[i].rules);
    evolution.push({
      version: versions[i].version,
      added: versions[i].rules.filter((r) => !prev.has(r)),
      removed: versions[i - 1].rules.filter((r) => !next.has(r)),
    });
  }
  return evolution;
}

export function aggregate(repo: string) {
  const cc = path.join(repo, ".codecouncil");
  const suggestions = readNdjson(path.join(cc, "suggestions.ndjsonl"));
  const outcomes = readNdjson(path.join(cc, "outcomes.ndjsonl"));
  const reflections = readNdjson(path.join(cc, "reflections.ndjsonl"));
  const observations = readNdjsonTail(path.join(cc, "observations.ndjsonl"));
  const delivered = readJson(path.join(cc, "delivered.json"));
  const heuristicsText = readText(path.join(cc, "heuristics.md"));

  const outcomeById = new Map(outcomes.map((o) => [o.suggestion_id, o]));

  // Peer-review feed: every SUGGESTION joined with its delivery + grade.
  const reviews = suggestions
    .filter((s) => s.verdict === "SUGGESTION")
    .map((s) => {
      const o = outcomeById.get(s.id);
      return {
        id: s.id,
        ts: s.ts,
        beat: s.beat,
        heuristicsVersion: s.heuristics_version ?? 0,
        file: s.suggestion?.file ?? "",
        line: s.suggestion?.line ?? null,
        issue: s.suggestion?.issue ?? "",
        rationale: s.suggestion?.rationale ?? "",
        severity: s.suggestion?.severity ?? "medium",
        deliveredVia: Object.keys(delivered[s.id] ?? {}),
        promptChars: s.prompt_chars ?? 0,
        reviewKind: s.review_kind ?? "beat",
        verification: s.verification ?? null,
        outcome: o?.outcome ?? null,
        evidence: o?.evidence ?? "",
        fileTouched: o?.file_touched ?? null,
      };
    })
    .reverse();

  // Verdict strip: every critic beat, PASS and all.
  const recentSuggestions = suggestions.slice(-120);
  const verdicts = recentSuggestions.map((s, k) => ({
    seq: suggestions.length - recentSuggestions.length + k,
    id: s.id,
    ts: s.ts,
    beat: s.beat,
    verdict: s.verdict,
    heuristicsVersion: s.heuristics_version ?? 0,
    nEvents: s.n_events ?? 0,
    severity: s.suggestion?.severity ?? null,
    reason: s.reason ? truncate(s.reason, 160) : null,
    error: s.error ? truncate(s.error, 120) : null,
    malformed: !!s.malformed,
  }));
  const malformedRecent = recentSuggestions.filter((s) => !!s.malformed).length;

  // seq is the event's index in the append-only file — a stable identity the
  // client uses to reveal newly-arrived events one at a time.
  const actBase = Math.max(0, observations.length - 120);
  const activity = observations.slice(-120).map((e, k) => ({
    seq: actBase + k,
    ts: e.ts,
    beat: e.beat,
    ...summarizeEvent(e),
  }));

  // History files named heuristics-v<N>-<ts>.md (any naming — count is enough).
  let historyCount = 0;
  try {
    historyCount = fs.readdirSync(path.join(cc, "heuristics-history")).filter((f) => f.endsWith(".md")).length;
  } catch {
    /* no history yet */
  }

  // Session receipts (Task 10): the human-facing artifact from a task
  // review. Newest 10, by mtime.
  let receipts: { name: string; mtime: string }[] = [];
  try {
    const receiptsDir = path.join(cc, "receipts");
    receipts = fs
      .readdirSync(receiptsDir)
      .filter((f) => f.endsWith(".md"))
      .map((name) => ({ name, mtimeMs: fs.statSync(path.join(receiptsDir, name)).mtimeMs }))
      .sort((a, b) => b.mtimeMs - a.mtimeMs)
      .slice(0, 10)
      .map((r) => ({ name: r.name, mtime: new Date(r.mtimeMs).toISOString() }));
  } catch {
    /* no receipts yet */
  }

  const graded = outcomes.filter((o) => GRADED.has(o.outcome));
  const accepted = graded.filter((o) => o.outcome === "accepted").length;
  const lastObs = observations.length ? observations[observations.length - 1].ts : null;
  const lastVerdict = suggestions.length ? suggestions[suggestions.length - 1].ts : null;
  const stateM = mtime(path.join(cc, "state.json"));
  // Both daemons advertise their configured interval; liveness = 3 missed beats.
  const observerIntervalS = readJson(path.join(cc, "state.json")).interval || 30;
  const criticIntervalS = readJson(path.join(cc, "critic-state.json")).interval || 45;

  return {
    now: new Date().toISOString(),
    repo: { path: repo, name: path.basename(repo) },
    live: {
      lastObservationTs: lastObs,
      lastVerdictTs: lastVerdict,
      observerIntervalS,
      observerLive:
        stateM !== null && Date.now() - stateM < Math.max(15_000, observerIntervalS * 3_000),
      criticLive: (() => {
        const m = mtime(path.join(cc, "critic-state.json"));
        return m !== null && Date.now() - m < Math.max(15_000, criticIntervalS * 3_000);
      })(),
    },
    stats: {
      beats: observations.length ? observations[observations.length - 1].beat : 0,
      events: observations.length,
      verdicts: suggestions.length,
      passes: suggestions.filter((s) => s.verdict === "PASS").length,
      suggestions: reviews.length,
      graded: graded.length,
      accepted,
      acceptanceRate: graded.length ? accepted / graded.length : null,
      heuristicsVersion: heuristicsVersion(heuristicsText),
      malformedRecent,
    },
    curve: buildCurve(suggestions, outcomes),
    reviews,
    verdicts,
    activity,
    receipts,
    heuristics: {
      version: heuristicsVersion(heuristicsText),
      rules: heuristicsRules(heuristicsText),
      historyCount,
      evolution: heuristicsEvolution(path.join(cc, "heuristics-history"), heuristicsText),
      rewrites: reflections
        .flatMap((r) => {
          // Plain rewrite records carry no "event" key (from/to/headline/stats
          // already shaped by reflector.rewrite.rewrite_record). The gate and
          // auto-rollback control-system events are separate row shapes
          // (rewrite_rejected / rollback) — synthesize a matching entry for
          // each instead of letting them render as {to: 0, blank headline}.
          if (r.event === "rewrite_rejected") {
            return [{
              ts: r.ts ?? "",
              from: r.from_version ?? 0,
              to: r.from_version ?? 0,
              headline: truncate(`rewrite rejected — ${String(r.note ?? "")}`, 140),
              stats: {},
            }];
          }
          if (r.event === "rollback") {
            return [{
              ts: r.ts ?? "",
              from: r.from_version ?? 0,
              to: (r.from_version ?? 0) + 1,
              headline: truncate(`rolled back to rules of v${r.restored_version_content_of ?? "?"}`, 140),
              stats: {},
            }];
          }
          if (r.event) return []; // unrecognized future event kind: skip, don't render garbage
          return [{
            ts: r.ts ?? "",
            from: r.from_version ?? 0,
            to: r.to_version ?? 0,
            headline: truncate(String(r.headline ?? ""), 140),
            stats: r.stats ?? {},
          }];
        })
        .reverse(),
    },
  };
}
