"""Delivery ledger: which suggestion ids went out via which channel.

Shape: {"<suggestion-id>": {"context": <epoch>, "block": <epoch>}}
"""

from __future__ import annotations

import json
from pathlib import Path


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
