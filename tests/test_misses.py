"""Tests for reflector.misses: detect PASSes followed by fix commits."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta
from reflector.misses import detect_misses, FIX_RE, LOOKBACK_S


NOW = 1000.0  # anchor time in seconds since epoch


def _iso(ts: float | None) -> str:
    """Convert timestamp to ISO 8601 string. Returns malformed string if None."""
    if ts is None:
        return "not-a-timestamp"
    return datetime.fromtimestamp(ts).isoformat()


def _pass_row(id="p1", ts=None, files=("a.py",)):
    """Helper to create a PASS suggestion row."""
    iso_ts = _iso(ts) if ts is not None else _iso(NOW)
    return {
        "id": id,
        "verdict": "PASS",
        "ts": iso_ts,
        "reviewed_files": list(files),
        "heuristics_version": 3,
    }


def _commit(ts, subject, diff):
    """Helper to create a commit event."""
    return {
        "type": "commit",
        "ts": _iso(ts),
        "payload": {"subjects": [f"abc123 {subject}"], "diff": diff},
    }


class TestMissDetection(unittest.TestCase):
    """Test the detect_misses() function against all eight cases."""

    def test_fix_subject_and_file_overlap_within_lookback(self):
        """Case 1: fix-subject + file overlap within lookback -> 1 miss."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(NOW + 100, "fix the thing", "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new")
        ]
        result = detect_misses(pass_rows, commit_events, set())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pass_id"], "p1")
        self.assertEqual(result[0]["file"], "a.py")
        self.assertEqual(result[0]["commit_subject"], "fix the thing")
        self.assertIn("a.py", result[0]["evidence"])
        self.assertIn("fix the thing", result[0]["evidence"])

    def test_fix_subject_no_file_overlap(self):
        """Case 2: fix-subject, NO file overlap -> no miss."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(NOW + 100, "fix something", "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old\n+new")
        ]
        result = detect_misses(pass_rows, commit_events, set())
        self.assertEqual(len(result), 0)

    def test_file_overlap_non_fix_subject(self):
        """Case 3: file overlap, non-fix subject ('add docs') -> no miss."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(
                NOW + 100,
                "add documentation",
                "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
            )
        ]
        result = detect_misses(pass_rows, commit_events, set())
        self.assertEqual(len(result), 0)

    def test_fix_commit_before_pass(self):
        """Case 4: fix commit BEFORE the pass -> no miss."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(
                NOW - 100,
                "fix something",
                "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
            )
        ]
        result = detect_misses(pass_rows, commit_events, set())
        self.assertEqual(len(result), 0)

    def test_fix_commit_past_lookback(self):
        """Case 5: fix commit past LOOKBACK_S -> no miss."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(
                NOW + LOOKBACK_S + 100,
                "fix something",
                "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
            )
        ]
        result = detect_misses(pass_rows, commit_events, set())
        self.assertEqual(len(result), 0)

    def test_pass_already_graded(self):
        """Case 6: pass_id already in already_graded -> no miss."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(
                NOW + 100,
                "fix something",
                "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
            )
        ]
        result = detect_misses(pass_rows, commit_events, {"p1"})
        self.assertEqual(len(result), 0)

    def test_two_matching_commits_first_wins(self):
        """Case 7: two matching commits -> exactly 1 miss (first)."""
        pass_rows = [_pass_row("p1", NOW, ("a.py",))]
        commit_events = [
            _commit(
                NOW + 100,
                "fix first",
                "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new",
            ),
            _commit(
                NOW + 200,
                "fix second",
                "--- a/a.py\n+++ b/a.py\n@@ -2 +2 @@\n-old2\n+new2",
            ),
        ]
        result = detect_misses(pass_rows, commit_events, set())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["commit_subject"], "fix first")

    def test_malformed_timestamp_skipped(self):
        """Case 8: malformed ts on either side -> skipped, no crash."""
        # Create p2 with a malformed ts by constructing it directly
        pass_rows = [
            _pass_row("p1", NOW, ("a.py",)),
            {"id": "p2", "verdict": "PASS", "ts": "not-a-valid-timestamp",
             "reviewed_files": ["a.py"], "heuristics_version": 3},
        ]
        commit_events = [
            _commit(NOW + 100, "fix first", "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"),
            {"type": "commit", "ts": "also-malformed",
             "payload": {"subjects": ["abc123 fix second"], "diff": "--- a/a.py\n+++ b/a.py\n@@ -2 +2 @@\n-old2\n+new2"}},
        ]
        # Should not crash, and should skip rows with bad timestamps
        result = detect_misses(pass_rows, commit_events, set())
        # Only p1 should be checked (p2 has malformed ts)
        # commit at NOW+100 should match, commit with malformed ts should be skipped
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pass_id"], "p1")


class TestFixRegex(unittest.TestCase):
    """Test the FIX_RE regex pattern."""

    def test_fix_variants(self):
        """Test that FIX_RE matches various fix-like subjects."""
        fix_words = [
            "fix the bug",
            "fixed an issue",
            "fixes something",
            "bug fix",
            "revert bad commit",
            "reverting",
            "correct typo",
            "correcting",
            "repair leak",
            "repairing",
            "hotfix critical",
            "hotfixing",
            "regress test",
            "regression",
        ]
        for subject in fix_words:
            self.assertTrue(FIX_RE.search(subject), f"Should match '{subject}'")

    def test_non_fix_subjects(self):
        """Test that FIX_RE doesn't match non-fix subjects."""
        non_fix = [
            "add feature",
            "refactor code",
            "update docs",
            "improve perf",
            "cleanup",
            "style changes",
        ]
        for subject in non_fix:
            self.assertFalse(FIX_RE.search(subject), f"Should not match '{subject}'")


if __name__ == "__main__":
    unittest.main()
