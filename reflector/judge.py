"""Grade delivered suggestions: eligibility, evidence bundling, prompt, parse."""

from __future__ import annotations

import json
import re
from datetime import datetime

ELIGIBLE_AFTER_S = 120      # let the coding agent react before grading
EVIDENCE_WINDOW_S = 900     # how far past delivery to look for evidence
UNDELIVERED_AFTER_S = 900   # past this with no delivery, grade "undelivered" for free
MAX_EVIDENCE_REASONING = 6
MAX_EVIDENCE_TOOL_CALLS = 8
MAX_EVIDENCE_DIFF_CHARS = 8_000

OUTCOMES = {"accepted", "rebutted", "ignored"}
REBUTTAL_MARKER = "COUNCIL-REBUTTAL"


def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def first_delivery(delivered: dict, sid: str) -> float | None:
    channels = delivered.get(sid) or {}
    return min(channels.values()) if channels else None


def pending(suggestions: list[dict], delivered: dict, graded_ids: set[str],
            now: float) -> tuple[list[dict], list[dict]]:
    """Split ungraded suggestions into (judge-with-model, undelivered-for-free)."""
    to_judge, undelivered = [], []
    for row in suggestions:
        sid = row.get("id")
        if row.get("verdict") != "SUGGESTION" or not sid or sid in graded_ids:
            continue
        ts = _epoch(row.get("ts", ""))
        if ts is None:
            continue
        d = first_delivery(delivered, sid)
        if d is None:
            if now - ts > UNDELIVERED_AFTER_S:
                undelivered.append(row)
        elif now - d >= ELIGIBLE_AFTER_S:
            to_judge.append(row)
    return to_judge, undelivered


def evidence(row: dict, delivery_ts: float, observations: list[dict]) -> str:
    """What happened in the window after this suggestion was delivered."""
    window = [
        o for o in observations
        if (t := _epoch(o.get("ts", ""))) is not None
        and delivery_ts <= t <= delivery_ts + EVIDENCE_WINDOW_S
    ]
    parts = ["CODING AGENT REASONING AFTER DELIVERY:"]
    reasoning = [o for o in window if o.get("type") == "reasoning"][:MAX_EVIDENCE_REASONING]
    parts += [f"- {o['payload']['text']}" for o in reasoning] or ["(none captured)"]
    parts.append("")
    # tool calls carry durable intent (commit messages, commands) even when
    # transcript prose is missing — include them as first-class evidence
    parts.append("CODING AGENT ACTIONS AFTER DELIVERY:")
    tools = [o for o in window if o.get("type") == "tool_call"][:MAX_EVIDENCE_TOOL_CALLS]
    for o in tools:
        inp = o["payload"].get("input", {})
        detail = inp.get("file_path") or inp.get("command") or ""
        parts.append(f"- {o['payload'].get('tool', '?')} {detail}".rstrip())
    if not tools:
        parts.append("(none captured)")
    parts.append("")
    parts.append("COMMITS AFTER DELIVERY:")
    commits = [o for o in window if o.get("type") == "commit"]
    if commits:
        for c in commits:
            parts += [f"- {s}" for s in c["payload"].get("subjects", [])]
            parts.append(c["payload"].get("diff", "")[: MAX_EVIDENCE_DIFF_CHARS // 2])
    else:
        parts.append("(none)")
    parts.append("")
    parts.append("LATEST DIFF AFTER DELIVERY:")
    diffs = [o for o in window if o.get("type") == "diff"]
    if diffs:
        diff = diffs[-1]["payload"].get("diff", "")
        parts.append(diff[:MAX_EVIDENCE_DIFF_CHARS] or "(no tracked changes)")
    else:
        parts.append("(no diff captured in window)")
    return "\n".join(parts)


def explicit_rebuttal(delivery_ts: float, observations: list[dict]) -> str | None:
    """Deterministic rebuttal: the coding agent used the COUNCIL-REBUTTAL marker
    (which the hook's injection text asks for) in post-delivery reasoning.
    Returns the stated reason, or None. No model call needed when this fires."""
    for o in observations:
        if o.get("type") != "reasoning":
            continue
        t = _epoch(o.get("ts", ""))
        if t is None or not (delivery_ts <= t <= delivery_ts + EVIDENCE_WINDOW_S):
            continue
        text = (o.get("payload") or {}).get("text", "")
        if REBUTTAL_MARKER in text:
            m = re.search(REBUTTAL_MARKER + r"\s*[:—–-]\s*(.+)", text)
            return (m.group(1).split("\n")[0].strip() if m else text)[:300]
    return None


def file_touched(row: dict, delivery_ts: float, observations: list[dict]) -> bool:
    """Model-free signal: did the flagged file show up in any post-delivery change?"""
    path = row["suggestion"]["file"]
    for o in observations:
        if o.get("type") not in ("diff", "commit"):
            continue
        t = _epoch(o.get("ts", ""))
        if t is None or not (delivery_ts <= t <= delivery_ts + EVIDENCE_WINDOW_S):
            continue
        payload = o.get("payload", {})
        if path in payload.get("diff", ""):
            return True
        if any(path in u for u in payload.get("untracked", [])):
            return True
        if any(path in k for k in payload.get("untracked_contents", {})):
            return True
    return False


def build_prompt(row: dict, evidence_text: str) -> str:
    s = row["suggestion"]
    loc = f"{s['file']}:{s['line']}" if s.get("line") else s["file"]
    return (
        "TASK: GRADE\n\n"
        f"SUGGESTION (delivered to the coding agent):\n"
        f"[{s['severity'].upper()}] {loc} — {s['issue']}\n"
        f"Rationale: {s.get('rationale', '')}\n\n"
        f"{evidence_text}\n\n"
        "Did the flagged code change in the suggested direction (accepted)? Did the "
        "agent explicitly engage with the suggestion and state disagreement with "
        "reasons — a statement alone counts, no code change required (rebutted)? "
        "Or did the agent neither change the code nor engage with it (ignored)? "
        "Reply with exactly one raw JSON object in this exact shape: "
        '{"outcome": "accepted" | "rebutted" | "ignored", "evidence": "<one sentence>"}'
    )


def parse_grade(raw: str) -> dict:
    """Strict parse; anything malformed grades as 'ignored' with the raw kept."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                outcome = obj.get("outcome") or obj.get("grade")  # models drift to the task name
                if outcome in OUTCOMES:
                    return {"outcome": outcome, "evidence": str(obj.get("evidence", ""))}
        except json.JSONDecodeError:
            pass
    return {"outcome": "ignored", "malformed": raw[:500]}
