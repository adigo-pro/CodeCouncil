"""Tests for evals.run.score_heuristics — the importable per-heuristics-text
scorer that both `python3 -m evals.run` and the reflector's rewrite gate
build on. All model calls are stubbed via CRITIC_CMD."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run import CASES_DIR, score_heuristics

# One call per case: reply PASS for the "clean" case (matched by marker text
# in the prompt), a suggestion JSON for the "flag" case.
PASS_THEN_SUGGESTION = """#!/bin/sh
if grep -q MARKER_PASS_CASE "$1"; then
  echo 'PASS'
else
  echo '{"file": "foo.py", "line": 1, "issue": "bug", "severity": "medium"}'
fi
"""


def _cases():
    return [
        {
            "name": "clean-case", "expected": "pass", "expect_files": [],
            "events": [{"type": "reasoning", "payload": {"text": "MARKER_PASS_CASE nothing wrong"}}],
            "latest_diff": None,
        },
        {
            "name": "flag-case", "expected": "flag", "expect_files": ["foo.py"],
            "events": [{"type": "reasoning", "payload": {"text": "MARKER_FLAG_CASE risky change"}}],
            "latest_diff": None,
        },
    ]


class TestScoreHeuristics(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.stub = Path(self.td.name) / "stub.sh"
        self.stub.write_text(PASS_THEN_SUGGESTION)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        os.environ["CRITIC_CMD"] = str(self.stub)

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def test_score_computed_from_pass_then_suggestion(self):
        result = score_heuristics("version: 1\n- flag risky changes\n", _cases())
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["score"], 1.0)
        self.assertEqual([r["ok"] for r in result["results"]], [True, True])
        self.assertEqual(result["results"][0]["verdict"], "PASS")
        self.assertEqual(result["results"][1]["verdict"], "SUGGESTION")

    def test_empty_cases_scores_zero_not_crash(self):
        result = score_heuristics("version: 1\n- x\n", [])
        self.assertEqual(result, {"score": 0.0, "n": 0, "results": []})


class TestCaseSchema(unittest.TestCase):
    def test_failure_mode_slices_well_formed(self):
        for p in sorted(CASES_DIR.glob("*.json")):
            case = json.loads(p.read_text())
            self.assertIn(case["expected"], {"flag", "pass"})
            if case["expected"] == "flag":
                self.assertTrue(case["expect_files"])


class TestLoadCasesTolerance(unittest.TestCase):
    def test_torn_harvested_case_is_skipped_not_fatal(self):
        # A torn/corrupt harvested case (shared dir, concurrent writers) must
        # be skipped — an uncaught JSONDecodeError propagates through the
        # rewrite gate and crash-loops the reflector daemon.
        import evals.run as run
        with tempfile.TemporaryDirectory() as td:
            harvested = Path(td) / "cases-harvested"
            harvested.mkdir()
            (harvested / "good.json").write_text(
                json.dumps({"name": "g", "expected": "pass", "expect_files": [],
                            "events": [], "latest_diff": None}), encoding="utf-8")
            (harvested / "torn.json").write_text('{"name": "t", "exp', encoding="utf-8")
            with mock.patch.object(run, "HARVESTED_CASES_DIR", harvested), \
                    mock.patch.object(run, "CASES_DIR", Path(td) / "empty"):
                cases = run.load_cases()  # must not raise
            self.assertEqual([c["name"] for c in cases], ["g"])


if __name__ == "__main__":
    unittest.main()
