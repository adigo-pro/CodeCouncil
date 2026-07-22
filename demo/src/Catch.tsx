/**
 * CodeCouncil README demo — the live catch sequence of 2026-07-21, recreated
 * in the product's OWN design system (ui/src): dark dashboard canvas, the
 * ActivityFeed ink card on the left, a ReviewPanel card on the right. Every
 * string is from the real run. The embedded payments.py deliberately contains
 * the claim-vs-code bug the sequence catches — it is demo specimen material.
 */
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// ---- ui/src/styles.css dark tokens (hex equivalents of the oklch values) ----
const C = {
  background: "#14161b",
  card: "#1b1e24",
  muted: "#23262e",
  mutedFg: "#9298a3",
  border: "#2c3039",
  fg: "#e6e8ec",
  ink: "#0f1115",
  ok: "#55d68d",
  warn: "#e8b45a",
  bad: "#ef7360",
  live: "#4ed885",
  // ActivityFeed row accents (the feed's actual tailwind classes)
  purple: "#d8b4fe",
  cyan: "#67e8f9",
  amber: "#fcd34d",
  green: "#4ade80",
  neutral400: "#9ca3af",
  neutral600: "#4b5563",
};
const MONO = '"JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace';
const SANS = 'Inter, ui-sans-serif, system-ui, -apple-system, sans-serif';

// ---- timeline (30fps) ----
const T = {
  titleEnd: 75,
  codeStart: 75,
  skipStart: 255,
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
  { t: "    Validates that amount_cents is a positive integer and raises", kind: "claim" },
  { t: "    ValueError on zero or negative amounts, so a refund can never", kind: "claim" },
  { t: '    be issued through the charge path."""' },
  { t: '    payload = {"amount": amount_cents, "capture": True}' },
  { t: "    if amount_cents < 0:", kind: "bug" },
  { t: '        payload["amount"] = 0', kind: "bug" },
  { t: "    return payload" },
];
const FEED = [
  { at: 95, glyph: "◆", color: C.purple, italic: true, t: "writing the payment helper for the checkout flow…" },
  { at: 140, glyph: "▸", color: C.cyan, t: "Write scratchtest/payments.py" },
  { at: 215, glyph: "±", color: C.green, t: "diff changed · 1 untracked file" },
];
const FINDING_BODY =
  "Docstring claims function raises ValueError for zero or negative amounts, but code does not raise and modifies payload instead.";
const PROOF = "✓ verified by repro: charge_amount_cents(-500) → no ValueError raised";
const INJECTED = [
  "Peer reviewer (CodeCouncil) flagged on recent changes:",
  "[MEDIUM] scratchtest/payments.py:7 — Docstring claims function",
  "raises ValueError… but code does not raise. Address if valid,",
  "or reply COUNCIL-REBUTTAL: <reason> to disagree.",
];
const FIX_ADDED = [
  "    if amount_cents <= 0:",
  '        raise ValueError(f"amount_cents must be positive")',
];

// ---- helpers ----
const Fade: React.FC<{ from: number; children: React.ReactNode; dur?: number }> = ({
  from, children, dur = 10,
}) => {
  const frame = useCurrentFrame();
  const o = interpolate(frame, [from, from + dur], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  if (frame < from) return null;
  return <div style={{ opacity: o }}>{children}</div>;
};
const typed = (text: string, frame: number, from: number, cps: number) =>
  frame <= from ? "" : text.slice(0, Math.floor((frame - from) * cps));

/** The dashboard card shell: rounded-3xl, border, card surface. */
const Card: React.FC<{ children: React.ReactNode; ink?: boolean; style?: React.CSSProperties }> = ({
  children, ink, style,
}) => (
  <div
    style={{
      flex: 1,
      borderRadius: 24,
      border: `1px solid ${C.border}`,
      background: ink ? C.ink : C.card,
      overflow: "hidden",
      display: "flex",
      flexDirection: "column",
      ...style,
    }}
  >
    {children}
  </div>
);

/** VerdictStrip: hollow PASS dots, then the filled SUGGESTION dot. */
const VerdictStrip: React.FC = () => {
  const frame = useCurrentFrame();
  const filled = frame >= T.findingStart;
  return (
    <div style={{ display: "flex", gap: 5, alignItems: "center" }}>
      {Array.from({ length: 9 }).map((_, i) => (
        <span key={i} style={{
          width: 8, height: 8, borderRadius: 4,
          border: `1px solid ${C.fg}40`,
        }} />
      ))}
      {filled && <span style={{ width: 8, height: 8, borderRadius: 4, background: C.fg }} />}
    </div>
  );
};

// ---- scenes ----
const Title: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sub = spring({ frame: frame - 34, fps, config: { damping: 200 } });
  const out = interpolate(frame, [T.titleEnd - 12, T.titleEnd], [1, 0], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{
      background: C.background, alignItems: "center", justifyContent: "center", opacity: out,
    }}>
      <div style={{ fontFamily: MONO, fontSize: 58, fontWeight: 700, color: C.fg, letterSpacing: -1 }}>
        {typed("CodeCouncil", frame, 6, 0.55)}
        <span style={{ color: C.live }}>▍</span>
      </div>
      <div style={{ fontFamily: SANS, fontSize: 21, color: C.mutedFg, marginTop: 16, opacity: sub }}>
        an AI peer reviewer for AI coding agents
      </div>
    </AbsoluteFill>
  );
};

const FeedCard: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const budget = Math.max(0, (frame - T.codeStart - 25) * 2.4);
  let used = 0;
  const fixP = spring({ frame: frame - T.fixStart, fps, config: { damping: 200 } });
  const showFix = frame >= T.fixStart;
  return (
    <Card ink>
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        borderBottom: "1px solid rgba(255,255,255,0.08)", padding: "12px 20px",
      }}>
        <span style={{ width: 6, height: 6, borderRadius: 3, background: C.live }} />
        <span style={{ fontFamily: MONO, fontSize: 13.5, color: C.neutral400 }}>
          observer — what the coding agent is doing
        </span>
      </div>
      <div style={{ padding: "16px 22px", fontFamily: MONO, fontSize: 14.5, lineHeight: 1.8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
          <span style={{ fontSize: 10, letterSpacing: 2, color: C.neutral600, textTransform: "uppercase" }}>
            beat 5734
          </span>
          <span style={{ height: 1, flex: 1, background: "rgba(255,255,255,0.07)" }} />
        </div>
        {FEED.map((l, i) => (
          <Fade key={i} from={l.at}>
            <div style={{ color: C.neutral400 }}>
              <span style={{ color: l.color, marginRight: 10 }}>{l.glyph}</span>
              <span style={{ fontStyle: l.italic ? "italic" : "normal", color: l.italic ? C.neutral400 : l.color }}>
                {l.t}
              </span>
            </div>
          </Fade>
        ))}
        <div style={{
          marginTop: 14, borderRadius: 12, background: "rgba(255,255,255,0.03)",
          border: "1px solid rgba(255,255,255,0.06)", padding: "12px 16px",
          fontSize: 13.5, lineHeight: 1.7, whiteSpace: "pre", paddingRight: 6,
        }}>
          {CODE.map((line, i) => {
            const remaining = Math.max(0, budget - used);
            const shown = line.t.slice(0, Math.max(0, Math.floor(remaining)));
            used += line.t.length || 1;
            const removed = showFix && line.kind === "bug";
            const isLastBug = line.kind === "bug" && CODE[i + 1]?.kind !== "bug";
            const color = line.kind === "claim" ? C.amber : line.kind === "def" ? C.cyan : "#c9cdd4";
            if (removed) {
              return (
                <React.Fragment key={i}>
                  <div style={{ color: C.bad, opacity: 1 - 0.6 * fixP, textDecoration: "line-through" }}>
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
              <div key={i} style={{ color }}>
                {shown}
                {shown.length > 0 && shown.length < line.t.length ? (
                  <span style={{ color: C.live }}>▍</span>
                ) : null}
              </div>
            );
          })}
        </div>
        <Fade from={T.injectStart} dur={12}>
          <div style={{
            marginTop: 14, border: `1px solid ${C.warn}44`, background: `${C.warn}0d`,
            borderRadius: 12, padding: "10px 14px", color: C.neutral400,
            fontSize: 12.5, lineHeight: 1.65, whiteSpace: "pre-wrap",
          }}>
            {INJECTED.join("\n")}
          </div>
        </Fade>
      </div>
    </Card>
  );
};

const ReviewCardPanel: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const bodyTyped = typed(FINDING_BODY, frame, T.findingStart + 14, 3.2);
  const stamp = spring({ frame: frame - T.proofStamp, fps, config: { damping: 14, mass: 0.6 } });
  return (
    <Card>
      <div style={{ padding: "20px 24px 0" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
          <div>
            <div style={{ fontFamily: SANS, fontSize: 18, fontWeight: 600, color: C.fg }}>
              Peer review
            </div>
            <div style={{ fontFamily: SANS, fontSize: 13, color: C.mutedFg, marginTop: 3 }}>
              9 quiet passes · 1 interruption worth making
            </div>
          </div>
          <VerdictStrip />
        </div>
      </div>

      <Fade from={T.findingStart} dur={8}>
        <div style={{
          margin: "18px 20px 0", borderRadius: 16, background: C.background,
          border: `1px solid ${C.border}`, padding: "16px 18px",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span style={{
              fontFamily: SANS, fontSize: 11, fontWeight: 600, letterSpacing: 1,
              textTransform: "uppercase", color: C.warn,
              border: `1px solid ${C.warn}66`, background: `${C.warn}0d`,
              borderRadius: 999, padding: "2px 10px",
            }}>
              medium
            </span>
            <span style={{ fontFamily: MONO, fontSize: 12.5, color: C.mutedFg }}>
              scratchtest/payments.py:7
            </span>
          </div>
          <div style={{
            fontFamily: SANS, fontSize: 15, lineHeight: 1.6, color: C.fg,
            marginTop: 10, minHeight: 72,
          }}>
            {bodyTyped}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
            {frame >= T.proofStamp && (
              <span style={{
                transform: `scale(${0.9 + 0.1 * stamp})`, transformOrigin: "left center",
                opacity: stamp, fontFamily: MONO, fontSize: 12,
                color: C.ok, background: `${C.ok}1a`, borderRadius: 999, padding: "3px 12px",
              }}>
                {PROOF}
              </span>
            )}
            <Fade from={T.deliverLine} dur={8}>
              <span style={{
                fontFamily: MONO, fontSize: 12, color: C.mutedFg,
                background: C.muted, borderRadius: 999, padding: "3px 12px",
              }}>
                → injected in context
              </span>
            </Fade>
          </div>
          <Fade from={T.passStart} dur={8}>
            <div style={{
              borderTop: `1px solid ${C.border}`, marginTop: 14, paddingTop: 12,
              fontFamily: SANS, fontSize: 13, fontStyle: "italic", color: C.mutedFg,
            }}>
              reflector: the flagged code changed in the suggested direction — graded{" "}
              <span style={{ color: C.ok, fontStyle: "normal", fontFamily: MONO }}>accepted</span>
            </div>
          </Fade>
        </div>
      </Fade>

      <Fade from={T.passStart + 20} dur={8}>
        <div style={{
          margin: "12px 20px", fontFamily: MONO, fontSize: 13.5, color: C.ok,
        }}>
          ✓ PASS — Docstring and code now match; issue resolved
        </div>
      </Fade>
    </Card>
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
    <div style={{
      position: "absolute", top: 22, left: "50%", transform: "translateX(-50%)",
      background: C.card, border: `1px solid ${C.border}`, color: C.mutedFg,
      borderRadius: 999, padding: "6px 18px", fontSize: 13.5, fontFamily: MONO, opacity: o,
    }}>
      ⏱ 84 seconds of real time
    </div>
  );
};

const EndCard: React.FC = () => {
  const frame = useCurrentFrame();
  if (frame < T.endStart) return null;
  const o = interpolate(frame, [T.endStart, T.endStart + 15], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{
      background: `${C.background}ee`, alignItems: "center", justifyContent: "center", opacity: o,
    }}>
      <div style={{ fontFamily: SANS, fontSize: 28, color: C.fg, fontWeight: 600 }}>
        catches the claim · proves it · sees the fix
      </div>
      <div style={{ fontFamily: MONO, fontSize: 18, color: C.ok, marginTop: 16 }}>
        github.com/adigo-tamu/CodeCouncil
      </div>
      <div style={{ fontFamily: SANS, fontSize: 13.5, color: C.mutedFg, marginTop: 10 }}>
        real sequence, real timings — Apache-2.0
      </div>
    </AbsoluteFill>
  );
};

export const Catch: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: C.background }}>
      {frame >= T.titleEnd - 12 && (
        <AbsoluteFill style={{ flexDirection: "row", gap: 16, padding: 20 }}>
          <FeedCard />
          <ReviewCardPanel />
        </AbsoluteFill>
      )}
      <TimeSkip />
      {frame < T.titleEnd && <Title />}
      <EndCard />
    </AbsoluteFill>
  );
};
