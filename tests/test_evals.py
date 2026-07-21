"""Tests for evals.run.score_heuristics — the importable per-heuristics-text
scorer that both `python3 -m evals.run` and the reflector's rewrite gate
build on. All model calls are stubbed via CRITIC_CMD."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run import score_heuristics

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


if __name__ == "__main__":
    unittest.main()
