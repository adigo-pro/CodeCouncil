"""Delivery ledger: which suggestion ids went out via which channel.

Shape: {"<suggestion-id>": {"context": <epoch>, "block": <epoch>}}

One key is reserved for a different kind of delivery: RECEIPTS_KEY holds
{"<receipt-filename>": <epoch announced>}. Suggestion ids are always 12 hex
chars (uuid4().hex[:12] in critic.main); RECEIPTS_KEY is the 8-char literal
string "receipts", which is not valid hex of that length, so it can never
collide with a real suggestion id.
"""

from __future__ import annotations

import json
from pathlib import Path

RECEIPTS_KEY = "receipts"


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
