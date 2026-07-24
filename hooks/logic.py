"""Pure decision logic for the peer-review hook.

`decide()` sees one Claude Code hook event plus the current suggestions and
delivery ledger, and returns (output-JSON-or-None, mutated ledger). No I/O —
everything here is unit-testable without a filesystem or a session.

Delivery rules:
  - medium/high suggestions -> injected as context after an edit (once each)
  - high suggestions        -> block completion at Stop (once each, never
                               when stop_hook_active is set)
  - suggestions older than TTL_SECONDS are never delivered
  - the newest receipt's test_integrity verdict == "weakened" -> block
    completion at Stop once (ledger key "test_integrity", keyed by receipt
    filename rather than suggestion id — Task 2). A pending high-severity
    suggestion block takes priority in the same Stop call; the test-integrity
    block is checked only once there is none. Never re-blocks the same
    receipt: a COUNCIL-REBUTTAL reply is graded later by the Reflector, not
    parsed here — the block-once ledger mark is what keeps Stop from
    interrupting a session twice for the same receipt.
"""

from __future__ import annotations

from datetime import datetime

from . import ledger as ledger_mod

TTL_SECONDS = 600
MAX_CONTEXT_ITEMS = 3

CONTEXT_SEVERITIES = {"medium", "high"}
BLOCK_SEVERITIES = {"high"}


def _age_ok(row: dict, now: float) -> bool:
    try:
        ts = datetime.fromisoformat(row["ts"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return False
    return 0 <= now - ts <= TTL_SECONDS


def _session_ok(row: dict, hook_session_id: str | None) -> bool:
    """A suggestion tagged with its source session must not leak into an
    unrelated session (dogfood-observed bug: reviewer session got implementer
    findings). No tag -> repo-wide (task reviews). A hook event missing
    session_id delivers anyway: never silently drop a finding over a missing
    field."""
    row_session = row.get("session")
    if not row_session:
        return True
    if not hook_session_id:
        return True
    return row_session == hook_session_id


def _pending(suggestions: list[dict], ledger: dict, channel: str,
             severities: set[str], now: float, hook_session_id: str | None) -> list[dict]:
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
            # the anchor's veto is overridden only by execution proof: a
            # prober-only finding (precision anchor PASSed, only the
            # recall-prober caught it) ships only when a repro actually
            # verified it. Absent "council" key -> unchanged behavior.
            and ((row.get("council") or {}).get("agreement") != "prober-only"
                 or (row.get("verification") or {}).get("status") == "verified")
            and _session_ok(row, hook_session_id)
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
        if v.get("repro"):
            # the receiving end is a coding AGENT, not a human — a runnable
            # command it can execute itself beats another line of prose. But
            # the command is model-authored from semi-trusted file content
            # (critic/verify.py) — phrase it as something to look at, not an
            # imperative a coding agent will reflexively execute in the real
            # repo.
            text += f" [suggested repro (review before running): {v['repro']}]"
    return text


def _pick_receipt(receipts: list[dict], ledger: dict, now: float) -> dict | None:
    """The newest not-yet-announced receipt, or None. `receipts` is caller-
    supplied data (filenames + paths, newest first) — no filesystem access
    here, that's peer_hook.py's job. Marks the ledger for whichever receipt
    it picks, same as a suggestion delivery does."""
    for r in receipts:
        name = r.get("name")
        if name and not ledger_mod.receipt_announced(ledger, name):
            ledger_mod.mark_receipt(ledger, name, now)
            return r
    return None


def _receipt_line(receipt: dict) -> str:
    path = receipt.get("path") or receipt.get("name")
    return f"CodeCouncil wrote a session receipt: {path} (claims vs verified — worth a look)"


def _test_integrity_block(receipts: list[dict], ledger: dict, now: float) -> dict | None:
    """Block Stop once when the newest receipt's mechanically-scored
    test_integrity verdict is "weakened". `receipts[0]` is caller-supplied,
    already-parsed data (peer_hook.py reads the receipt file and calls
    critic.receipt.parse_test_integrity — decide() stays pure/fs-free).
    A receipt with no "test_integrity" key (written before this feature) or
    any verdict other than "weakened" is silent."""
    if not receipts:
        return None
    latest = receipts[0]
    name = latest.get("name")
    ti = latest.get("test_integrity") or {}
    if not name or ti.get("verdict") != "weakened":
        return None
    if ledger_mod.test_integrity_blocked(ledger, name):
        return None
    ledger_mod.mark_test_integrity(ledger, name, now)
    removed = ti.get("asserts_removed", 0)
    added = ti.get("asserts_added", 0)
    return {
        "decision": "block",
        "reason": (
            f"CodeCouncil peer reviewer: this session weakened its tests "
            f"({removed} assertions removed, {added} added) — restore them, "
            "or explain with a line `COUNCIL-REBUTTAL: <your reason>`."
        ),
    }


def decide(event: dict, suggestions: list[dict], ledger: dict, now: float,
          receipts: list[dict] = ()) -> dict | None:
    """Returns hook output JSON (or None for silence). May mark the ledger.

    `receipts` is an optional, caller-supplied list of {"name", "path"} dicts
    for files in .codecouncil/receipts/, newest first — default empty so
    every existing call site/test is unaffected. At most one is announced
    per event, and each at most once ever (tracked in the ledger's reserved
    "receipts" key — see hooks.ledger).
    """
    hook = event.get("hook_event_name")
    hook_session_id = event.get("session_id")

    # session start delivers like an edit does: findings that landed between
    # sessions reach the next session immediately instead of expiring
    if hook in ("PostToolUse", "UserPromptSubmit"):
        pending = _pending(suggestions, ledger, "context", CONTEXT_SEVERITIES, now, hook_session_id)
        receipt = _pick_receipt(receipts, ledger, now)
        if not pending and not receipt:
            return None
        blocks = []
        if pending:
            shown = pending[:MAX_CONTEXT_ITEMS]
            for row in shown:
                ledger_mod.mark(ledger, row["id"], "context", now)
            lines = "\n".join(f"- {_describe(r)}" for r in shown)
            blocks.append(
                "Peer reviewer (CodeCouncil) flagged on recent changes:\n"
                f"{lines}\n"
                "Address each if valid. If you disagree, reply with a line "
                "`COUNCIL-REBUTTAL: <your reason>` so the disagreement is recorded."
            )
        if receipt:
            blocks.append(_receipt_line(receipt))
        return {
            "hookSpecificOutput": {
                "hookEventName": hook,
                "additionalContext": "\n".join(blocks),
            }
        }

    # Stop does NOT get a receipt announcement: Claude Code's Stop hook JSON
    # only supports decision:"block"/reason (no additionalContext channel),
    # so there is no non-blocking way to surface a receipt here. It just
    # waits for the next PostToolUse/UserPromptSubmit, same as any finding
    # that arrives between sessions.
    if hook == "Stop":
        if event.get("stop_hook_active"):
            return None
        pending = _pending(suggestions, ledger, "block", BLOCK_SEVERITIES, now, hook_session_id)
        if pending:
            row = pending[0]  # one interruption at a time; the rest wait for the next Stop
            ledger_mod.mark(ledger, row["id"], "block", now)
            return {
                "decision": "block",
                "reason": (
                    "CodeCouncil peer reviewer has an unresolved finding: "
                    f"{_describe(row)}. Fix it if you agree, or reply with a line "
                    "`COUNCIL-REBUTTAL: <your reason>` if you disagree — then finish."
                ),
            }
        # no pending suggestion block this turn: check the test-integrity gate
        return _test_integrity_block(receipts, ledger, now)

    return None
