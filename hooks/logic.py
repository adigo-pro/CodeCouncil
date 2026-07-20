"""Pure decision logic for the peer-review hook.

`decide()` sees one Claude Code hook event plus the current suggestions and
delivery ledger, and returns (output-JSON-or-None, mutated ledger). No I/O —
everything here is unit-testable without a filesystem or a session.

Delivery rules:
  - medium/high suggestions -> injected as context after an edit (once each)
  - high suggestions        -> block completion at Stop (once each, never
                               when stop_hook_active is set)
  - suggestions older than TTL_SECONDS are never delivered
"""

from __future__ import annotations

from datetime import datetime

TTL_SECONDS = 600
MAX_CONTEXT_ITEMS = 3

CONTEXT_SEVERITIES = {"medium", "high"}
BLOCK_SEVERITIES = {"high"}

from . import ledger as ledger_mod


def _age_ok(row: dict, now: float) -> bool:
    try:
        ts = datetime.fromisoformat(row["ts"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return False
    return 0 <= now - ts <= TTL_SECONDS


def _pending(suggestions: list[dict], ledger: dict, channel: str,
             severities: set[str], now: float) -> list[dict]:
    out = []
    for row in suggestions:
        s = row.get("suggestion") or {}
        if (
            row.get("verdict") == "SUGGESTION"
            and row.get("id")
            and s.get("severity") in severities
            and _age_ok(row, now)
            and not ledger_mod.delivered(ledger, row["id"], channel)
            # findings the critic itself refuted during verification never ship
            and (row.get("verification") or {}).get("status") != "refuted"
        ):
            out.append(row)
    return out


def _describe(row: dict) -> str:
    s = row["suggestion"]
    loc = f"{s['file']}:{s['line']}" if s.get("line") else s["file"]
    text = f"[{s['severity'].upper()}] {loc} — {s['issue']}"
    if s.get("rationale"):
        text += f" (why: {s['rationale']})"
    v = row.get("verification") or {}
    if v.get("status") == "verified":
        text += f" [verified by repro: {v.get('note', '')}]"
    return text


def decide(event: dict, suggestions: list[dict], ledger: dict, now: float) -> dict | None:
    """Returns hook output JSON (or None for silence). May mark the ledger."""
    hook = event.get("hook_event_name")

    # session start delivers like an edit does: findings that landed between
    # sessions reach the next session immediately instead of expiring
    if hook in ("PostToolUse", "UserPromptSubmit"):
        pending = _pending(suggestions, ledger, "context", CONTEXT_SEVERITIES, now)
        if not pending:
            return None
        shown = pending[:MAX_CONTEXT_ITEMS]
        for row in shown:
            ledger_mod.mark(ledger, row["id"], "context", now)
        lines = "\n".join(f"- {_describe(r)}" for r in shown)
        return {
            "hookSpecificOutput": {
                "hookEventName": hook,
                "additionalContext": (
                    "Peer reviewer (CodeCouncil) flagged on recent changes:\n"
                    f"{lines}\n"
                    "Address each if valid, or briefly say why you disagree."
                ),
            }
        }

    if hook == "Stop":
        if event.get("stop_hook_active"):
            return None
        pending = _pending(suggestions, ledger, "block", BLOCK_SEVERITIES, now)
        if not pending:
            return None
        row = pending[0]  # one interruption at a time; the rest wait for the next Stop
        ledger_mod.mark(ledger, row["id"], "block", now)
        return {
            "decision": "block",
            "reason": (
                "CodeCouncil peer reviewer has an unresolved finding: "
                f"{_describe(row)}. Fix it if you agree, or state briefly why "
                "you disagree — then finish."
            ),
        }

    return None
