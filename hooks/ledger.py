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

# Suggestion-id delivery marks are TTL-pruned so the file stays bounded. This
# TTL is delivery-RECORD retention, NOT delivery freshness: freshness (don't
# deliver a stale finding) is governed by hooks.logic._age_ok on the row's own
# ts. The record must outlive the reflector's grading horizon
# (reflector.judge.UNDELIVERED_AFTER_S = 900s) plus its poll interval, or a
# genuinely-delivered finding whose mark was pruned first grades "undelivered"
# and drops out of the acceptance metric. 3600s clears that with margin.
# (A separate local constant, not an import: hooks.logic imports hooks.ledger,
# so importing back here would cycle.)
LEDGER_TTL_SECONDS = 3600

# The three reserved keys encode "once ever" facts (this receipt was announced;
# this weakened-test receipt already blocked Stop; this session spent its one
# done-gate wait). TTL-pruning them was a real bug: a mark dropped after
# LEDGER_TTL_SECONDS let receipts re-announce, weakened receipts re-block Stop,
# and the gate re-wait every window. So they are NEVER TTL-pruned — bounded by
# COUNT instead (newest kept), which keeps the file bounded without expiring a
# once-ever fact. Generous vs the on-disk receipt cap (RECEIPTS_KEEP=50).
RESERVED_KEYS = (RECEIPTS_KEY, TEST_INTEGRITY_KEY, GATE_KEY)
RESERVED_KEEP = 200


def load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _pruned(ledger: dict, now: float, ttl: float = LEDGER_TTL_SECONDS) -> dict:
    """Bound delivered.json before a save. Suggestion-id keys
    (`{"context": ts, "block": ts}`) are pruned by TTL; the three reserved keys
    (each `{name-or-session: ts}`) are pruned by COUNT — newest RESERVED_KEEP
    leaves kept — because their marks must not expire (see RESERVED_KEYS). A
    top-level key left with no leaves is dropped so the file doesn't accumulate
    empty shells. Malformed (non-dict) entries are dropped rather than raising."""
    pruned: dict = {}
    for key, leaves in ledger.items():
        if not isinstance(leaves, dict):
            continue
        valid = {leaf: ts for leaf, ts in leaves.items()
                 if isinstance(ts, (int, float))}
        if key in RESERVED_KEYS:
            if len(valid) > RESERVED_KEEP:
                newest = sorted(valid.items(), key=lambda kv: kv[1], reverse=True)
                valid = dict(newest[:RESERVED_KEEP])
            kept = valid
        else:
            kept = {leaf: ts for leaf, ts in valid.items() if now - ts <= ttl}
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
