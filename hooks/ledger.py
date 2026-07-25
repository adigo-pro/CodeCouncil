"""Delivery ledger: which suggestion ids went out via which channel.

Shape: {"<suggestion-id>": {"context": <epoch>, "block": <epoch>}}

Three keys are reserved for other kinds of delivery. RECEIPTS_KEY and
TEST_INTEGRITY_KEY each hold {"<receipt-filename>": <epoch>}: RECEIPTS_KEY
tracks announced receipts, TEST_INTEGRITY_KEY tracks which receipt already
blocked Stop once for a "weakened" test-integrity verdict (Task 2). GATE_KEY
holds {"<session-id>": <epoch>}: which sessions already spent their one
done-gate wait (Task 1 — peer_hook.py polls at most once per session while
Stop is held open for the critic to catch up). Suggestion ids are always 12
hex chars (uuid4().hex[:12] in critic.main); "receipts" (8 chars),
"test_integrity" (14 chars) and "gate" (4 chars) are not valid hex of those
lengths, so none can ever collide with a real suggestion id.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.store import write_json_atomic

RECEIPTS_KEY = "receipts"
TEST_INTEGRITY_KEY = "test_integrity"
GATE_KEY = "gate"

# delivered.json gets one key per suggestion/receipt/gated-session and is
# never otherwise pruned, so a long session's ledger grows unbounded. This
# mirrors hooks.logic.TTL_SECONDS (the delivery freshness window a stale
# suggestion is judged against) but is a separate local constant rather than
# an import: hooks.logic already imports hooks.ledger ("from . import ledger
# as ledger_mod"), so importing logic.TTL_SECONDS back here would cycle.
LEDGER_TTL_SECONDS = 600


def load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _pruned(ledger: dict, now: float, ttl: float = LEDGER_TTL_SECONDS) -> dict:
    """Drop stale leaf entries before a save. Every top-level key in this
    ledger -- a suggestion id (`{"context": ts, "block": ts}`) or one of the
    three reserved keys RECEIPTS_KEY/TEST_INTEGRITY_KEY/GATE_KEY (each
    `{name-or-session: ts}`) -- shares the same {leaf: epoch} nested shape,
    so one pass prunes both without special-casing which keys are reserved.
    A top-level key left with no leaves after pruning is dropped entirely,
    which is what actually keeps the file bounded rather than accumulating
    empty shells forever. Malformed (non-dict) entries are dropped rather
    than raising."""
    pruned: dict = {}
    for key, leaves in ledger.items():
        if not isinstance(leaves, dict):
            continue
        kept = {
            leaf: ts for leaf, ts in leaves.items()
            if isinstance(ts, (int, float)) and now - ts <= ttl
        }
        if kept:
            pruned[key] = kept
    return pruned


def save(path: Path, ledger: dict) -> None:
    write_json_atomic(path, _pruned(ledger, time.time()))


def mark(ledger: dict, suggestion_id: str, channel: str, now: float) -> None:
    ledger.setdefault(suggestion_id, {})[channel] = now


def delivered(ledger: dict, suggestion_id: str, channel: str) -> bool:
    return channel in ledger.get(suggestion_id, {})


def receipt_announced(ledger: dict, filename: str) -> bool:
    return filename in ledger.get(RECEIPTS_KEY, {})


def mark_receipt(ledger: dict, filename: str, now: float) -> None:
    ledger.setdefault(RECEIPTS_KEY, {})[filename] = now


def test_integrity_blocked(ledger: dict, filename: str) -> bool:
    return filename in ledger.get(TEST_INTEGRITY_KEY, {})


def mark_test_integrity(ledger: dict, filename: str, now: float) -> None:
    ledger.setdefault(TEST_INTEGRITY_KEY, {})[filename] = now


def gate_used(ledger: dict, session_key: str) -> bool:
    return session_key in ledger.get(GATE_KEY, {})


def mark_gate(ledger: dict, session_key: str, now: float) -> None:
    ledger.setdefault(GATE_KEY, {})[session_key] = now
