"""Persistent tick-to-tick state: JSONL byte offsets, last diff hash, beat counter."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class State:
    offsets: dict[str, int] = field(default_factory=dict)
    last_diff_hash: str | None = None
    beat: int = 0

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return cls(
                    offsets=raw.get("offsets", {}),
                    last_diff_hash=raw.get("last_diff_hash"),
                    beat=raw.get("beat", 0),
                )
            except (json.JSONDecodeError, OSError):
                pass  # corrupt state: start fresh rather than crash the daemon
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "offsets": self.offsets,
                    "last_diff_hash": self.last_diff_hash,
                    "beat": self.beat,
                }
            ),
            encoding="utf-8",
        )
