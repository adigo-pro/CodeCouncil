"""Capture what actually changed: git diff + untracked files in the watched repo."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from core.redact import redact

DIFF_MAX_CHARS = 50_000
NEW_FILE_MAX_CHARS = 4_000
NEW_FILES_TOTAL_CHARS = 20_000
EXCLUDED_PREFIXES = (".codecouncil/", ".claude/", ".git/")


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


def _read_untracked(repo: Path, paths: list[str]) -> dict[str, str]:
    """Contents of new (untracked) text files, capped, so the critic can see them."""
    out: dict[str, str] = {}
    total = 0
    for p in paths:
        if p.startswith(EXCLUDED_PREFIXES) or total >= NEW_FILES_TOTAL_CHARS:
            continue
        try:
            data = (repo / p).read_bytes()[: NEW_FILE_MAX_CHARS * 2]
        except OSError:
            continue
        if b"\0" in data:
            continue  # binary
        text = redact(data.decode("utf-8", errors="replace"))
        if len(text) > NEW_FILE_MAX_CHARS:
            text = text[:NEW_FILE_MAX_CHARS] + "\n… [truncated]"
        out[p] = text
        total += len(text)
    return out


def capture(repo: Path) -> dict:
    """Snapshot uncommitted work. Falls back gracefully in a repo with no commits."""
    # -U8: enough surrounding source that the critic judges hunks in context
    diff = _git(repo, "diff", "-U8", "HEAD") or _git(repo, "diff", "-U8")
    stat = _git(repo, "diff", "HEAD", "--stat") or _git(repo, "diff", "--stat")
    untracked = [
        p for p in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines() if p
    ]
    diff = redact(diff)
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + f"\n… [diff truncated, {len(diff)} chars total]"
    return {
        "diff": diff,
        "stat": stat.strip(),
        "untracked": untracked,
        "untracked_contents": _read_untracked(repo, untracked),
    }


COMMIT_DIFF_MAX_CHARS = 20_000


def head(repo: Path) -> str | None:
    out = _git(repo, "rev-parse", "HEAD").strip()
    return out or None


def capture_commits(repo: Path, old: str, new: str) -> dict:
    """What landed between two HEADs — so committed work stays reviewable."""
    subjects = [s for s in _git(repo, "log", "--format=%h %s", f"{old}..{new}").splitlines() if s]
    diff = redact(_git(repo, "diff", "-U8", old, new))
    if len(diff) > COMMIT_DIFF_MAX_CHARS:
        diff = diff[:COMMIT_DIFF_MAX_CHARS] + f"\n… [commit diff truncated, {len(diff)} chars total]"
    stat = _git(repo, "diff", old, new, "--stat").strip()
    return {"from": old, "to": new, "subjects": subjects, "diff": diff, "stat": stat}


def fingerprint(snapshot: dict) -> str:
    h = hashlib.sha256()
    h.update(snapshot["diff"].encode("utf-8", errors="replace"))
    h.update("\0".join(snapshot["untracked"]).encode("utf-8", errors="replace"))
    for path, text in sorted(snapshot.get("untracked_contents", {}).items()):
        h.update(path.encode("utf-8", errors="replace"))
        h.update(text.encode("utf-8", errors="replace"))
    return h.hexdigest()
