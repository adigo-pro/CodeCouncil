"""Event schema and NDJSON sink — the Observer's output contract with the Critic."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REASONING = "reasoning"
TOOL_CALL = "tool_call"
DIFF = "diff"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Event:
    beat: int
    type: str
    payload: dict[str, Any]
    session: str | None = None
    ts: str = field(default_factory=now_iso)


class EventLog:
    """Appends events as NDJSON lines to .codecouncil/observations.ndjsonl."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, events: list[Event]) -> None:
        if not events:
            return
        with self.path.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
