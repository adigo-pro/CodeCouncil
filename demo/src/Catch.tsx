/**
 * CodeCouncil README demo — a faithful recreation of the live catch sequence
 * from 2026-07-21 (see docs/benchmarks + the session receipts). Every string
 * below is taken from the real run: the planted payments.py, the critic's
 * actual finding, the real verification note, and the real resolution PASS.
 */
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// ---- the repo's own dashboard palette ----
const C = {
  bg: "#101216",
  pane: "#16181d",
  chrome: "#1d2027",
  border: "#262a33",
  text: "#d4d7dd",
  dim: "#6f7683",
  faint: "#464c58",
  cyan: "#7dd3fc",
  purple: "#c4b5fd",
  amber: "#fbbf24",
  green: "#4ade80",
  red: "#f87171",
};
const MONO = '"JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace';

// ---- timeline (30fps) ----
const T = {
  titleEnd: 75,
  codeStart: 75,
  codeEnd: 255,
  skipStart: 255,
  skipEnd: 315,
  findingStart: 315,
  proofStamp: 400,
  deliverLine: 440,
  injectStart: 465,
  fixStart: 530,
  passStart: 615,
  endStart: 705,
};

// ---- real material from the live run ----
const CODE: Array<{ t: string; kind?: "claim" | "bug" | "def" }> = [
  { t: "def charge_amount_cents(amount_cents: int) -> dict:", kind: "def" },
  { t: '    """Validate and build a charge payload.' },
  { t: "" },
  { t: "    Validates that amount_cents is a positive integer and raises", kind: "claim" },
  { t: "    ValueError on zero or negative amounts, so a refund can never", kind: "claim" },
  { t: '    be issued through the charge path."""' },
  { t: '    payload = {"amount": amount_cents, "capture": True}' },
  { t: "    if amount_cents < 0:", kind: "bug" },
  { t: '        payload["amount"] = 0', kind: "bug" },
  { t: "    return payload" },
];

const OBSERVER_LINES = [
  { at: 100, color: C.dim, t: "♥ beat 5734 · 2 event(s)" },
  { at: 130, color: C.purple, t: "🧠 6507564d  writing the payment helper…" },
  { at: 165, color: C.cyan, t: "🔧 6507564d  Write scratchtest/payments.py" },
  { at: 205, color: C.dim, t: "Δ  diff changed · 1 untracked file" },
  { at: 235, color: C.faint, t: "· beat 5735 · judging…" },
];

const FINDING_HEAD = "SUGGESTION [medium] scratchtest/payments.py";
const FINDING_BODY =
  "Docstring claims function raises ValueError for zero or negative amounts, but code does not raise and modifies payload instead.";
const PROOF =
  "verified by repro: called charge_amount_cents(-500) → returned payload, no ValueError";
const DELIVERED = "→ injected into the coding agent's context";
const INJECTED = [
  "Peer reviewer (CodeCouncil) flagged on recent changes:",
  "[MEDIUM] scratchtest/payments.py:7 — Docstring claims function",
  "raises ValueError… but code does not raise. Address if valid,",
  "or reply COUNCIL-REBUTTAL: <reason> to disagree.",
];
const FIX_REMOVED = ["    if amount_cents < 0:", '        payload["amount"] = 0'];
const FIX_ADDED = [
  "    if amount_cents <= 0:",
  '        raise ValueError(f"amount_cents must be positive")',
];
const RESOLUTION = "✓ PASS — Docstring and code now match; issue resolved";

// ---- helpers ----
const Fade: React.FC<{ from: number; children: React.ReactNode; dur?: number }> = ({
  from,
  children,
  dur = 10,
}) => {
  const frame = useCurrentFrame();
  const o = interpolate(frame, [from, from + dur], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  if (frame < from) return null;
  return <div style={{ opacity: o }}>{children}</div>;
};

/** Slice text by frame for a typewriter feel. */
const typed = (text: string, frame: number, from: number, cps: number) =>
  frame <= from ? "" : text.slice(0, Math.floor((frame - from) * cps));

const PaneChrome: React.FC<{ title: string; live?: boolean }> = ({ title, live }) => (
  <div
    style={{
      background: C.chrome,
      borderBottom: `1px solid ${C.border}`,
      padding: "10px 16px",
      display: "flex",
      alignItems: "center",
      gap: 10,
      fontSize: 15,
      color: C.dim,
      letterSpacing: 0.5,
    }}
  >
    <span style={{ display: "flex", gap: 6 }}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          style={{ width: 10, height: 10, borderRadius: 5, background: C.border }}
        />
      ))}
    </span>
    <span>{title}</span>
    {live && (
      <span
        style={{ width: 8, height: 8, borderRadius: 4, background: C.green, marginLeft: "auto" }}
      />
    )}
  </div>
);

// ---- scenes ----
const Title: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const name = typed("CodeCouncil", frame, 6, 0.6);
  const sub = spring({ frame: frame - 40, fps, config: { damping: 200 } });
  const out = interpolate(frame, [T.titleEnd - 12, T.titleEnd], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill
      style={{
        background: C.bg,
        alignItems: "center",
        justifyContent: "center",
        fontFamily: MONO,
        opacity: out,
      }}
    >
      <div style={{ fontSize: 64, fontWeight: 700, color: C.text, letterSpacing: -1 }}>
        {name}
        <span style={{ color: C.green }}>▍</span>
      </div>
      <div style={{ fontSize: 22, color: C.dim, marginTop: 18, opacity: sub }}>
        an AI peer reviewer for AI coding agents
      </div>
    </AbsoluteFill>
  );
};

const LeftPane: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  // total chars typed across CODE lines
  const budget = Math.max(0, (frame - T.codeStart) * 2.2);
  let used = 0;
  const fixP = spring({ frame: frame - T.fixStart, fps, config: { damping: 200 } });
  const showFix = frame >= T.fixStart;
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", background: C.pane }}>
      <PaneChrome title="claude code — agent session" />
      <div style={{ padding: "18px 22px", fontSize: 16.5, lineHeight: 1.75, whiteSpace: "pre" }}>
        <div style={{ color: C.faint, marginBottom: 8 }}>$ # the agent writes a payment helper…</div>
        {CODE.map((line, i) => {
          const remaining = Math.max(0, budget - used);
          const shown = line.t.slice(0, Math.max(0, Math.floor(remaining)));
          used += line.t.length || 1;
          const removed = showFix && line.kind === "bug";
          const color =
            line.kind === "claim" ? C.amber : line.kind === "def" ? C.cyan : C.text;
          const isLastBug = line.kind === "bug" && CODE[i + 1]?.kind !== "bug";
          if (removed) {
            return (
              <React.Fragment key={i}>
                <div style={{ color: C.red, opacity: 1 - 0.65 * fixP, textDecoration: "line-through" }}>
                  {line.t}
                </div>
                {isLastBug && (
                  <div style={{ opacity: fixP }}>
                    {FIX_ADDED.map((l, j) => (
                      <div key={j} style={{ color: C.green }}>{`+ ${l}`}</div>
                    ))}
                  </div>
                )}
              </React.Fragment>
            );
          }
          return (
            <div key={i} style={{ color, opacity: line.kind === "claim" ? 0.95 : 0.9 }}>
              {shown}
              {shown.length > 0 && shown.length < line.t.length ? (
                <span style={{ color: C.green }}>▍</span>
              ) : null}
            </div>
          );
        })}
        <Fade from={T.injectStart} dur={12}>
          <div
            style={{
              marginTop: 16,
              border: `1px solid ${C.amber}55`,
              background: `${C.amber}0d`,
              borderRadius: 8,
              padding: "10px 14px",
              color: C.dim,
              fontSize: 14.5,
              lineHeight: 1.6,
              whiteSpace: "pre-wrap",
            }}
          >
            {INJECTED.join("\n")}
          </div>
        </Fade>
      </div>
    </div>
  );
};

const RightPane: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const bodyTyped = typed(FINDING_BODY, frame, T.findingStart + 14, 3.2);
  const stamp = spring({ frame: frame - T.proofStamp, fps, config: { damping: 14, mass: 0.6 } });
  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        background: C.pane,
        borderLeft: `1px solid ${C.border}`,
      }}
    >
      <PaneChrome title="codecouncil — critic" live />
      <div style={{ padding: "18px 22px", fontSize: 15.5, lineHeight: 1.9 }}>
        {OBSERVER_LINES.map((l, i) => (
          <Fade key={i} from={l.at}>
            <div style={{ color: l.color }}>{l.t}</div>
          </Fade>
        ))}

        <Fade from={T.findingStart} dur={6}>
          <div style={{ marginTop: 14 }}>
            <span
              style={{
                color: C.red,
                border: `1px solid ${C.red}66`,
                borderRadius: 999,
                padding: "1px 10px",
                fontSize: 12.5,
                marginRight: 10,
                letterSpacing: 1,
              }}
            >
              MEDIUM
            </span>
            <span style={{ color: C.amber, fontWeight: 700 }}>{FINDING_HEAD}</span>
          </div>
          <div style={{ color: C.text, marginTop: 6, maxWidth: 560, whiteSpace: "pre-wrap" }}>
            {bodyTyped}
          </div>
        </Fade>

        {frame >= T.proofStamp && (
          <div
            style={{
              transform: `scale(${0.9 + 0.1 * stamp})`,
              transformOrigin: "left center",
              opacity: stamp,
              marginTop: 12,
              display: "inline-block",
              border: `1px solid ${C.green}77`,
              background: `${C.green}12`,
              color: C.green,
              borderRadius: 8,
              padding: "8px 14px",
              fontSize: 14.5,
              maxWidth: 560,
            }}
          >
            ✓ {PROOF}
          </div>
        )}

        <Fade from={T.deliverLine}>
          <div style={{ color: C.cyan, marginTop: 12 }}>{DELIVERED}</div>
        </Fade>

        <Fade from={T.passStart} dur={8}>
          <div style={{ color: C.green, marginTop: 18, fontWeight: 700 }}>{RESOLUTION}</div>
          <div style={{ color: C.faint, marginTop: 4, fontSize: 13.5 }}>
            graded `accepted` by the reflector → becomes training signal
          </div>
        </Fade>
      </div>
    </div>
  );
};

const TimeSkip: React.FC = () => {
  const frame = useCurrentFrame();
  if (frame < T.skipStart || frame > T.findingStart + 30) return null;
  const o = interpolate(
    frame,
    [T.skipStart, T.skipStart + 10, T.findingStart + 15, T.findingStart + 30],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  return (
    <div
      style={{
        position: "absolute",
        top: 26,
        left: "50%",
        transform: "translateX(-50%)",
        background: C.chrome,
        border: `1px solid ${C.border}`,
        color: C.dim,
        borderRadius: 999,
        padding: "6px 18px",
        fontSize: 14,
        fontFamily: MONO,
        opacity: o,
      }}
    >
      ⏱ 84 seconds of real time
    </div>
  );
};

const EndCard: React.FC = () => {
  const frame = useCurrentFrame();
  if (frame < T.endStart) return null;
  const o = interpolate(frame, [T.endStart, T.endStart + 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill
      style={{
        background: `${C.bg}e6`,
        alignItems: "center",
        justifyContent: "center",
        fontFamily: MONO,
        opacity: o,
      }}
    >
      <div style={{ fontSize: 30, color: C.text, fontWeight: 700 }}>
        catches the claim · proves it · sees the fix
      </div>
      <div style={{ fontSize: 19, color: C.green, marginTop: 16 }}>
        github.com/adigo-tamu/CodeCouncil
      </div>
      <div style={{ fontSize: 14, color: C.dim, marginTop: 10 }}>
        real sequence, real timings — Apache-2.0
      </div>
    </AbsoluteFill>
  );
};

export const Catch: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: MONO }}>
      {frame >= T.titleEnd - 12 && (
        <AbsoluteFill style={{ flexDirection: "row" }}>
          <LeftPane />
          <RightPane />
        </AbsoluteFill>
      )}
      <TimeSkip />
      {frame < T.titleEnd && <Title />}
      <EndCard />
    </AbsoluteFill>
  );
};
