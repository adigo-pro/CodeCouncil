"""Shared file plumbing for the three daemons: NDJSON rows and startup waits.

Every reader must tolerate a partial trailing line (files are appended
mid-write) and skip unparseable lines rather than crash.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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


def read_tail_rows(path: Path, max_bytes: int = 2_000_000) -> list[dict]:
    """Like read_rows, but parses only the last `max_bytes` of the file — for
    append-only logs (observations) that grow without bound over a session.

    A possibly-truncated first line is dropped. Safe for the callers here, whose
    time windows (grading evidence, task-review recency) only ever reach back
    minutes; 2 MB is far more than any such window's worth of events, while
    keeping per-pass work flat no matter how long the session runs.
    """
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial line the seek landed inside
            data = f.read()
    except OSError:
        return []
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json_atomic(path: Path, obj, *, indent: int | None = None) -> None:
    """Write obj as JSON to path atomically (tmp in same dir + os.replace),
    so a crash mid-write can never leave a torn/empty file that a reader
    would treat as corrupt. Same durability discipline as core.config's
    save_config / update_env_key and core.knowledge.add_fact: write to a
    mkstemp'd file in the SAME directory (so os.replace is same-filesystem
    and therefore atomic), then replace the target in one syscall.

    `indent` is optional (default None = compact, matching every existing
    caller's prior output) for the rare human-edited target (e.g. a Claude
    Code settings.json) that wants pretty-printed JSON instead."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


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
