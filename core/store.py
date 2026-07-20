"""Shared file plumbing for the three daemons: NDJSON rows and startup waits.

Every reader must tolerate a partial trailing line (files are appended
mid-write) and skip unparseable lines rather than crash.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def wait_for(path: Path, message: str, once: bool, poll_s: float = 2.0) -> bool:
    """Block until `path` exists. In --once mode, error out instead of waiting."""
    if path.exists():
        return True
    if once:
        print(f"error: {path} not found — {message}", file=sys.stderr)
        return False
    print(f"waiting: {message} ({path})…")
    while not path.exists():
        time.sleep(poll_s)
    return True
