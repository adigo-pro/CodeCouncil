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
        # decode with errors="replace" (not read_text, which raises
        # UnicodeDecodeError): the file is appended mid-write, so the trailing
        # line can be torn mid-multibyte-character — and this repo's rows are
        # full of multibyte content (every «REDACTED:…» marker, every
        # `… [N chars total]`). read_tail_rows already decodes this way; a
        # torn byte must skip a line, never crash the reflector.
        lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
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
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())  # durability: survive power loss, not just process crash
        os.replace(tmp, path)
        tmp = None  # replaced — nothing to clean up
    finally:
        # if json.dump raised (unserializable obj) the tmp file was never
        # replaced; unlink it so failures don't litter `.codecouncil/` with
        # orphaned tmp*.tmp files next to the state they failed to write.
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (tmp in same dir + os.replace + fsync),
    the text sibling of write_json_atomic. For files a crash mid-write could
    corrupt that another process reads whole — heuristics-history archives
    (the rollback restore source), knowledge.md, receipts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
