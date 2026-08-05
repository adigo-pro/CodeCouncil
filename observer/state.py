"""Persistent tick-to-tick state: JSONL byte offsets, last diff hash, beat counter."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.store import write_json_atomic


@dataclass
class State:
    offsets: dict[str, int] = field(default_factory=dict)
    last_diff_hash: str | None = None
    beat: int = 0
    interval: float = 0.0  # advertised so the dashboard can pace its countdown
    last_head: str | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    offsets = raw.get("offsets", {})
                    return cls(
                        # valid JSON that isn't the shape we expect (a hand
                        # edit, a foreign writer) must rebuild, not crash: a
                        # non-dict `offsets` would raise later in collect().
                        offsets=offsets if isinstance(offsets, dict) else {},
                        last_diff_hash=raw.get("last_diff_hash"),
                        beat=raw.get("beat", 0),
                        interval=raw.get("interval", 0.0),
                        last_head=raw.get("last_head"),
                    )
            except (json.JSONDecodeError, OSError):
                pass  # corrupt state: start fresh rather than crash the daemon
        return cls()

    def save(self, path: Path) -> None:
        write_json_atomic(
            path,
            {
                "offsets": self.offsets,
                "last_diff_hash": self.last_diff_hash,
                "beat": self.beat,
                "interval": self.interval,
                "last_head": self.last_head,
            },
        )
