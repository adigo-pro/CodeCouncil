"""Detect missed catches: PASS verdicts whose reviewed files were revised by fix commits.

A false 'missed' grade poisons the eval harvest downstream — precision matters more
than recall. Both conditions must hold: (1) fix-shaped commit subject AND (2) file overlap
in the commit diff. One miss per PASS (first matching commit wins). commit_events must be
time-ordered (append-only log order); "first matching commit" means list order.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from observer.gitwatch import _touched_paths

# Deliberately no bare "regress*" alternative (dogfood false-miss,
# 2026-07-21): a test-only commit subject that merely mentions "regression"
# without describing an actual fix (e.g. "cover primary-ERROR axis and
# render regression") matched and produced false "missed" grades + noise
# harvested eval cases. A genuine regression fix virtually always also says
# fix/repair/correct — "fix regression in parser" still matches via "fix" —
# so precision wins over the marginal recall a bare "regress*" would add.
FIX_RE = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|bug(?:s)?|bugfix(?:es)?|revert(?:s|ed|ing)?|"
    r"correct(?:s|ed|ing|ion)?|repair(?:s|ed|ing)?|hotfix(?:es)?)\b",
    re.I
)
LOOKBACK_S = 3600  # one hour


# Paths never eligible for miss-grading: CodeCouncil's own learning corpus and
# runtime state. A fix commit that touches harvested eval cases is housekeeping
# of the loop's own files — grading a PASS as having "missed" a bug in the
# corpus produced live self-referential noise cases (observed 2026-07-22).
MISS_EXCLUDED_PREFIXES = ("evals/cases-harvested/", ".codecouncil/")


def _miss_eligible(path: str) -> bool:
    return not path.startswith(MISS_EXCLUDED_PREFIXES)


def _paths_match(reviewed: str, touched: str) -> bool:
    """True if `reviewed` and `touched` name the same file: exact (relative)
    path equality, OR matching basenames but ONLY when at least one side has
    no directory component. That bare-name tolerance exists for a
    staging-path prefix (e.g. 'underreview/d4ab-app.py' vs 'app.py') the same
    way critic/main.py's normalize_file already does, and for a bare name
    matching a directory-qualified one (e.g. 'app.py' vs 'src/app.py') — but
    it must NOT fire when BOTH sides are directory-qualified: two files that
    merely share a common basename (e.g. 'critic/main.py' vs
    'codecouncil/main.py') are not the same file, and matching them on
    basename alone (the old rule) produced a live false 'missed' grade —
    a fix commit touching codecouncil/main.py wrongly graded a PASS that
    reviewed critic/main.py, purely on the 'main.py' collision. Common
    filenames (main.py, utils.py, index.js, ...) make that systematic.
    This is also NOT substring containment: the older bidirectional
    `a in b or b in a` check wrongly matched 'utils.py' against
    'tests/test_utils.py' (one string contains the other); basename
    comparison alone already rejects that pair."""
    if reviewed == touched:
        return True
    reviewed_path = Path(reviewed)
    touched_path = Path(touched)
    if reviewed_path.name != touched_path.name:
        return False
    reviewed_is_bare = str(reviewed_path.parent) == "."
    touched_is_bare = str(touched_path.parent) == "."
    return reviewed_is_bare or touched_is_bare


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

        reviewed_files = [f for f in pass_row.get("reviewed_files", []) if _miss_eligible(f)]
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

            # Check if commit modifies any reviewed files (corpus/state paths
            # excluded on both sides — see MISS_EXCLUDED_PREFIXES)
            touched = [t for t in _touched_paths(commit_diff) if _miss_eligible(t)]
            matching_file = None
            for reviewed_file in reviewed_files:
                for touched_file in touched:
                    if _paths_match(reviewed_file, touched_file):
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
