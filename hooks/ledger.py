"""Delivery ledger: which suggestion ids went out via which channel.

Shape: {"<suggestion-id>": {"context": <epoch>, "block": <epoch>}}

Two keys are reserved for other kinds of delivery, each holding
{"<receipt-filename>": <epoch>}: RECEIPTS_KEY tracks announced receipts,
TEST_INTEGRITY_KEY tracks which receipt already blocked Stop once for a
"weakened" test-integrity verdict (Task 2). Suggestion ids are always 12 hex
chars (uuid4().hex[:12] in critic.main); "receipts" (8 chars) and
"test_integrity" (14 chars) are not valid hex of those lengths, so neither
can ever collide with a real suggestion id.
"""

from __future__ import annotations

import json
from pathlib import Path

RECEIPTS_KEY = "receipts"
TEST_INTEGRITY_KEY = "test_integrity"


def load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")


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
