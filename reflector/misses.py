"""Detect missed catches: PASS verdicts whose reviewed files were revised by fix commits.

A false 'missed' grade poisons the eval harvest downstream — precision matters more
than recall. Both conditions must hold: (1) fix-shaped commit subject AND (2) file overlap
in the commit diff. One miss per PASS (first matching commit wins). commit_events must be
time-ordered (append-only log order); "first matching commit" means list order.
"""

from __future__ import annotations

import re
from datetime import datetime

from observer.gitwatch import _touched_paths

FIX_RE = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|bug(?:s)?|bugfix(?:es)?|revert(?:s|ed|ing)?|"
    r"correct(?:s|ed|ing|ion)?|repair(?:s|ed|ing)?|hotfix(?:es)?|"
    r"regress(?:es|ed|ion|ions)?)\b",
    re.I
)
LOOKBACK_S = 3600  # one hour


def _epoch(iso: str) -> float | None:
    """Parse ISO 8601 timestamp to Unix epoch. Returns None on malformed input."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def detect_misses(
    pass_rows: list[dict], commit_events: list[dict], already_graded: set[str]
) -> list[dict]:
    """Detect PASS verdicts followed by fix commits that revised reviewed files.

    Args:
        pass_rows: List of suggestion rows with verdict="PASS" and reviewed_files.
        commit_events: List of commit event dicts with type="commit", ts, payload.subjects/diff.
        already_graded: Set of pass_ids to skip (already processed).

    Returns:
        List of miss dicts: {"pass_id": str, "file": str, "commit_subject": str, "evidence": str}
    """
    misses = []

    for pass_row in pass_rows:
        # Skip non-PASS rows or already-graded IDs
        if pass_row.get("verdict") != "PASS":
            continue
        pass_id = pass_row.get("id")
        if not pass_id or pass_id in already_graded:
            continue

        # Parse PASS timestamp
        pass_ts = _epoch(pass_row.get("ts", ""))
        if pass_ts is None:
            continue

        reviewed_files = pass_row.get("reviewed_files", [])
        if not reviewed_files:
            continue

        # Find first matching commit
        for commit_event in commit_events:
            if commit_event.get("type") != "commit":
                continue

            payload = commit_event.get("payload", {})
            commit_diff = payload.get("diff", "")
            subjects = payload.get("subjects", [])

            # Parse commit timestamp
            commit_ts = _epoch(commit_event.get("ts", ""))
            if commit_ts is None:
                continue

            # Check if commit is within lookback window after PASS
            if commit_ts <= pass_ts or commit_ts > pass_ts + LOOKBACK_S:
                continue

            # Find first subject that is fix-shaped
            fix_subject = None
            for subject in subjects:
                if FIX_RE.search(subject):
                    fix_subject = subject
                    break

            if not fix_subject:
                continue

            # Check if commit modifies any reviewed files
            touched = _touched_paths(commit_diff)
            matching_file = None
            for reviewed_file in reviewed_files:
                for touched_file in touched:
                    if reviewed_file in touched_file or touched_file in reviewed_file:
                        matching_file = reviewed_file
                        break
                if matching_file:
                    break

            if not matching_file:
                continue

            # Found a miss: fix commit within window that touched a reviewed file
            # Extract commit message (after hash) from the fix subject line
            parts = fix_subject.split(None, 1)
            commit_subject = parts[1] if len(parts) > 1 else parts[0]
            evidence = f"file={matching_file}, commit_subject={commit_subject}"
            misses.append(
                {
                    "pass_id": pass_id,
                    "file": matching_file,
                    "commit_subject": commit_subject,
                    "evidence": evidence,
                }
            )
            break  # One miss per PASS (first matching commit wins)

    return misses
