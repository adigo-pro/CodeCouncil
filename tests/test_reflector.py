"""Reflector tests: eligibility, grading parse, rewrite guardrails, report math."""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reflector import judge, rewrite
from reflector.report import build_rows, consistent

NOW = time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def sugg(sid="s1", ts=None, version=1):
    return {"id": sid, "ts": _iso(NOW - 400 if ts is None else ts), "verdict": "SUGGESTION",
            "heuristics_version": version,
            "suggestion": {"file": "a.py", "line": 3, "severity": "high",
                           "issue": "bug", "rationale": "r"}}


class TestGradePendingBoundedReads(unittest.TestCase):
    """suggestions.ndjsonl grows unbounded over a session; grade_pending only
    needs recent rows to find newly-gradeable suggestions, so it must read it
    via read_tail_rows. outcomes.ndjsonl's graded_ids dedup set must stay a
    full read_rows — losing an old graded id would re-grade an old suggestion."""

    def test_suggestions_bounded_outcomes_unbounded(self):
        import reflector.main as main_mod

        with tempfile.TemporaryDirectory() as td:
            cc = Path(td)
            tail_calls = []
            rows_calls = []
            orig_tail, orig_rows = main_mod.read_tail_rows, main_mod.read_ndjson

            def tracking_tail(path, *a, **k):
                tail_calls.append(path)
                return orig_tail(path, *a, **k)

            def tracking_rows(path, *a, **k):
                rows_calls.append(path)
                return orig_rows(path, *a, **k)

            with mock.patch.object(main_mod, "read_tail_rows", tracking_tail), \
                 mock.patch.object(main_mod, "read_ndjson", tracking_rows):
                main_mod.grade_pending(cc)

            self.assertIn(cc / "suggestions.ndjsonl", tail_calls)
            self.assertIn(cc / "outcomes.ndjsonl", rows_calls)
            self.assertNotIn(cc / "suggestions.ndjsonl", rows_calls)


class TestPending(unittest.TestCase):
    def test_delivered_and_mature_is_judged(self):
        delivered = {"s1": {"context": NOW - 300}}
        to_judge, undelivered = judge.pending([sugg()], delivered, set(), NOW)
        self.assertEqual([r["id"] for r in to_judge], ["s1"])
        self.assertEqual(undelivered, [])

    def test_recently_delivered_waits(self):
        delivered = {"s1": {"context": NOW - 30}}
        to_judge, undelivered = judge.pending([sugg()], delivered, set(), NOW)
        self.assertEqual((to_judge, undelivered), ([], []))

    def test_never_delivered_becomes_undelivered_after_timeout(self):
        old = sugg(ts=NOW - 1000)
        to_judge, undelivered = judge.pending([old], {}, set(), NOW)
        self.assertEqual(to_judge, [])
        self.assertEqual([r["id"] for r in undelivered], ["s1"])

    def test_already_graded_skipped(self):
        delivered = {"s1": {"context": NOW - 300}}
        to_judge, undelivered = judge.pending([sugg()], delivered, {"s1"}, NOW)
        self.assertEqual((to_judge, undelivered), ([], []))


class TestEvidence(unittest.TestCase):
    def test_window_filters_and_bundles(self):
        d = NOW - 300
        obs = [
            {"ts": _iso(d - 60), "type": "diff", "payload": {"diff": "OLD"}},
            {"ts": _iso(d + 60), "type": "reasoning", "payload": {"text": "fixing the bug"}},
            {"ts": _iso(d + 70), "type": "tool_call",
             "payload": {"tool": "Bash", "input": {"command": "git commit -m 'deliberate: keep as is'"}}},
            {"ts": _iso(d + 90), "type": "diff", "payload": {"diff": "+if b == 0: return None"}},
        ]
        text = judge.evidence(sugg(), d, obs)
        self.assertIn("fixing the bug", text)
        self.assertIn("if b == 0", text)
        self.assertIn("deliberate: keep as is", text)
        self.assertNotIn("OLD", text)


class TestParseGrade(unittest.TestCase):
    def test_valid(self):
        g = judge.parse_grade('{"outcome": "accepted", "evidence": "diff added check"}')
        self.assertEqual(g["outcome"], "accepted")

    def test_fenced(self):
        g = judge.parse_grade('```json\n{"outcome": "rebutted", "evidence": "e"}\n```')
        self.assertEqual(g["outcome"], "rebutted")

    def test_grade_key_alias_accepted(self):
        g = judge.parse_grade('{"grade": "accepted"}')
        self.assertEqual(g["outcome"], "accepted")
        self.assertNotIn("malformed", g)

    def test_malformed_is_ignored(self):
        for raw in ("The agent accepted it.", '{"outcome": "maybe"}', ""):
            g = judge.parse_grade(raw)
            self.assertEqual(g["outcome"], "ignored", raw)
            self.assertIn("malformed", g)


class TestRewriteGuardrails(unittest.TestCase):
    def test_should_rewrite_threshold_and_force(self):
        outcomes = [{"outcome": "accepted"}, {"outcome": "ignored"}]
        self.assertFalse(rewrite.should_rewrite(outcomes, 0, force=False))
        self.assertTrue(rewrite.should_rewrite(outcomes, 0, force=True))
        outcomes.append({"outcome": "rebutted"})
        self.assertTrue(rewrite.should_rewrite(outcomes, 0, force=False))
        self.assertFalse(rewrite.should_rewrite(outcomes, 3, force=False))

    def test_undelivered_does_not_count_toward_threshold(self):
        outcomes = [{"outcome": "undelivered"}] * 5
        self.assertFalse(rewrite.should_rewrite(outcomes, 0, force=False))

    def test_validate_rejects_bad_outputs(self):
        self.assertIsNotNone(rewrite.validate("", 2))
        self.assertIsNotNone(rewrite.validate("version: 3\n- rule", 2))
        self.assertIsNotNone(rewrite.validate("Here is the file:\nversion: 2", 2))
        self.assertIsNotNone(rewrite.validate("```\nversion: 2\n```", 2))
        self.assertIsNotNone(rewrite.validate("version: 2\n" + "- r\n" * 50, 2))
        self.assertIsNone(rewrite.validate("version: 2\n- keep flagging intent mismatches", 2))

    def test_rewrite_record_diffs_and_headline(self):
        old = "version: 1\n- keep this rule\n- drop this rule\n"
        new = "version: 2\n- keep this rule\n- brand new rule\n  with continuation\n"
        rec = rewrite.rewrite_record(old, new, 1, [{"outcome": "accepted"}, {"outcome": "ignored"}])
        self.assertEqual((rec["from_version"], rec["to_version"]), (1, 2))
        self.assertEqual(rec["added"], ["brand new rule with continuation"])
        self.assertEqual(rec["removed"], ["drop this rule"])
        self.assertEqual(rec["headline"], "brand new rule with continuation")
        self.assertEqual(rec["stats"]["accepted"], 1)

    def test_rewrite_record_reword_only(self):
        rec = rewrite.rewrite_record("version: 1\n- a\n", "version: 2\n- a\n", 1, [])
        self.assertEqual(rec["headline"], "reworded")

    def test_apply_archives_and_swaps(self):
        with tempfile.TemporaryDirectory() as td:
            h = Path(td) / "heuristics.md"
            h.write_text("version: 1\n- old rule\n")
            archive = rewrite.apply(h, "version: 2\n- new rule", "version: 1\n- old rule\n", 1)
            self.assertEqual(h.read_text(), "version: 2\n- new rule\n")
            self.assertEqual(archive.read_text(), "version: 1\n- old rule\n")
            self.assertEqual(archive.name, "v1.md")


class TestReport(unittest.TestCase):
    def test_acceptance_math_per_version(self):
        suggestions = [sugg("a", version=1), sugg("b", version=1), sugg("c", version=2)]
        outcomes = [
            {"suggestion_id": "a", "outcome": "accepted", "heuristics_version": 1},
            {"suggestion_id": "b", "outcome": "ignored", "heuristics_version": 1},
            {"suggestion_id": "c", "outcome": "accepted", "heuristics_version": 2},
        ]
        rows = build_rows(suggestions, outcomes)
        v1, v2 = rows[0], rows[1]
        self.assertEqual((v1["suggested"], v1["accepted"], v1["acceptance"]), (2, 1, 0.5))
        self.assertEqual((v2["suggested"], v2["acceptance"]), (1, 1.0))

    def test_xcheck_consistency_rules(self):
        self.assertTrue(consistent({"outcome": "accepted", "file_touched": True}))
        self.assertFalse(consistent({"outcome": "accepted", "file_touched": False}))
        self.assertTrue(consistent({"outcome": "ignored", "file_touched": False}))
        self.assertFalse(consistent({"outcome": "ignored", "file_touched": True}))
        self.assertTrue(consistent({"outcome": "rebutted", "file_touched": False}))
        self.assertIsNone(consistent({"outcome": "accepted"}))  # legacy rows: no signal
        self.assertIsNone(consistent({"outcome": "undelivered", "file_touched": True}))

    def test_file_touched_signal(self):
        d = NOW - 300
        obs = [
            {"ts": _iso(d - 60), "type": "diff", "payload": {"diff": "+++ b/a.py"}},  # pre-delivery
            {"ts": _iso(d + 60), "type": "diff", "payload": {"diff": "+++ b/other.py"}},
        ]
        self.assertFalse(judge.file_touched(sugg(), d, obs))
        obs.append({"ts": _iso(d + 90), "type": "diff",
                    "payload": {"diff": "", "untracked_contents": {"a.py": "x = 1"}}})
        self.assertTrue(judge.file_touched(sugg(), d, obs))


    def test_explicit_rebuttal_marker(self):
        d = NOW - 300
        obs = [
            {"ts": _iso(d - 60), "type": "reasoning",
             "payload": {"text": "COUNCIL-REBUTTAL: too early, before delivery"}},
            {"ts": _iso(d + 60), "type": "reasoning",
             "payload": {"text": "Thinking about it.\nCOUNCIL-REBUTTAL: guard exists on line 2\nmore text"}},
        ]
        self.assertEqual(judge.explicit_rebuttal(d, obs), "guard exists on line 2")
        # marker before delivery only -> None
        self.assertIsNone(judge.explicit_rebuttal(d, obs[:1]))
        # no marker at all -> None
        self.assertIsNone(judge.explicit_rebuttal(
            d, [{"ts": _iso(d + 60), "type": "reasoning", "payload": {"text": "I disagree"}}]))

    def test_xcheck_column_math(self):
        rows = build_rows(
            [sugg("a"), sugg("b")],
            [{"suggestion_id": "a", "outcome": "accepted", "heuristics_version": 1, "file_touched": True},
             {"suggestion_id": "b", "outcome": "ignored", "heuristics_version": 1, "file_touched": True}],
        )
        self.assertEqual(rows[0]["xcheck"], 0.5)
        self.assertEqual(len(rows[0]["inconsistent"]), 1)

    def test_undelivered_not_in_acceptance(self):
        rows = build_rows([sugg("a")], [{"suggestion_id": "a", "outcome": "undelivered",
                                         "heuristics_version": 1}])
        self.assertIsNone(rows[0]["acceptance"])
        self.assertEqual(rows[0]["undelivered"], 1)


if __name__ == "__main__":
    unittest.main()
