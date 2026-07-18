"""Capture what actually changed: git diff + untracked files in the watched repo."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

DIFF_MAX_CHARS = 50_000


def _git(repo: Path, *args: str) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return res.stdout if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def capture(repo: Path) -> dict:
    """Snapshot uncommitted work. Falls back gracefully in a repo with no commits."""
    diff = _git(repo, "diff", "HEAD") or _git(repo, "diff")
    stat = _git(repo, "diff", "HEAD", "--stat") or _git(repo, "diff", "--stat")
    untracked = [
        p for p in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines() if p
    ]
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + f"\n… [diff truncated, {len(diff)} chars total]"
    return {"diff": diff, "stat": stat.strip(), "untracked": untracked}


def fingerprint(snapshot: dict) -> str:
    h = hashlib.sha256()
    h.update(snapshot["diff"].encode("utf-8", errors="replace"))
    h.update("\0".join(snapshot["untracked"]).encode("utf-8", errors="replace"))
    return h.hexdigest()
