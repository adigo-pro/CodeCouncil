"""Reflector tests: eligibility, grading parse, rewrite guardrails, report math."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reflector import judge, rewrite
from reflector.report import build_mode_rows, build_rows, build_rule_rows, consistent
from tests.harvest_isolation import HarvestIsolatedTestCase

NOW = time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def sugg(sid="s1", ts=None, version=1, rule=None, failure_mode=None):
    return {"id": sid, "ts": _iso(NOW - 400 if ts is None else ts), "verdict": "SUGGESTION",
            "heuristics_version": version,
            "suggestion": {"file": "a.py", "line": 3, "severity": "high",
                           "issue": "bug", "rationale": "r", "rule": rule,
                           "failure_mode": failure_mode}}


class TestGradePendingBoundedReads(HarvestIsolatedTestCase):
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

    def test_untracked_file_contents_included(self):
        # a real fix living in an UNTRACKED file (never a tracked diff) must
        # still reach the grading model, or it gets under-credited as "ignored"
        d = NOW - 300
        obs = [
            {"ts": _iso(d + 60), "type": "diff", "payload": {
                "diff": "", "untracked_contents": {"new_file.py": "def fixed(): return 1"}}},
        ]
        text = judge.evidence(sugg(), d, obs)
        self.assertIn("NEW/UNTRACKED FILE CONTENTS AFTER DELIVERY:", text)
        self.assertIn("new_file.py", text)
        self.assertIn("def fixed(): return 1", text)

    def test_touched_contents_also_included(self):
        d = NOW - 300
        obs = [
            {"ts": _iso(d + 60), "type": "diff", "payload": {
                "diff": "", "touched_contents": {"a.py": "def touched(): return 2"}}},
        ]
        text = judge.evidence(sugg(), d, obs)
        self.assertIn("def touched(): return 2", text)

    def test_newfile_section_is_none_when_absent(self):
        d = NOW - 300
        obs = [{"ts": _iso(d + 60), "type": "diff", "payload": {"diff": "+something"}}]
        text = judge.evidence(sugg(), d, obs)
        idx = text.index("NEW/UNTRACKED FILE CONTENTS AFTER DELIVERY:")
        self.assertIn("(none)", text[idx:idx + 80])

    def test_newfile_section_capped(self):
        d = NOW - 300
        obs = [
            {"ts": _iso(d + 60), "type": "diff", "payload": {
                "diff": "", "untracked_contents": {"big.py": "x" * 20000}}},
        ]
        text = judge.evidence(sugg(), d, obs)
        idx = text.index("NEW/UNTRACKED FILE CONTENTS AFTER DELIVERY:")
        section = text[idx:]
        self.assertLessEqual(len(section) - len("NEW/UNTRACKED FILE CONTENTS AFTER DELIVERY:\n"),
                             judge.MAX_EVIDENCE_NEWFILE_CHARS + 20)


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

    def test_rewrite_prompt_includes_per_rule_stats(self):
        current = "version: 3\n- rule one\n- rule two\n- rule three"
        outcomes = [
            {"outcome": "accepted", "issue": "x", "heuristics_version": 3, "rule": 3},
            {"outcome": "rebutted", "issue": "y", "heuristics_version": 3, "rule": 3},
        ]
        text = rewrite.build_prompt(current, 3, outcomes)
        self.assertIn("R3: ", text)
        self.assertIn("2 graded, 1 accepted, 1 rebutted", text)
        self.assertIn("Prefer dropping or sharpening rules with rebuttals/ignores; "
                      "preserve rules with accepts.", text)

    def test_rewrite_prompt_includes_per_mode_stats(self):
        current = "version: 3\n- rule one\n- rule two\n- rule three"
        outcomes = [
            {"outcome": "accepted", "issue": "x", "heuristics_version": 3, "failure_mode": "claim-drift"},
            {"outcome": "rebutted", "issue": "y", "heuristics_version": 3, "failure_mode": "claim-drift"},
        ]
        text = rewrite.build_prompt(current, 3, outcomes)
        self.assertIn("claim-drift: ", text)
        self.assertIn("2 graded, 1 accepted, 1 rebutted", text)
        self.assertIn(
            "Modes with accepts are where your independent perspective pays — keep "
            "hunting them; modes with only rebuttals/ignores need sharper rules, not "
            "abandonment.",
            text,
        )

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

    def test_missed_not_in_acceptance_rate(self):
        pass_row = {"id": "p1", "verdict": "PASS", "heuristics_version": 3,
                   "reviewed_files": ["a.py"]}
        rows = build_rows([pass_row], [{"suggestion_id": "p1", "outcome": "missed",
                                        "heuristics_version": 3}])
        self.assertIsNone(rows[0]["acceptance"])
        self.assertEqual(rows[0]["missed"], 1)

    def test_build_rule_rows_aggregates_per_version_rule(self):
        suggestions = [sugg("a", version=1, rule=3), sugg("b", version=1, rule=3),
                      sugg("c", version=1, rule=None)]
        outcomes = [
            {"suggestion_id": "a", "outcome": "accepted", "heuristics_version": 1, "rule": 3},
            {"suggestion_id": "b", "outcome": "rebutted", "heuristics_version": 1, "rule": 3},
            {"suggestion_id": "c", "outcome": "ignored", "heuristics_version": 1, "rule": None},
        ]
        rows = build_rule_rows(suggestions, outcomes)
        r3 = next(r for r in rows if r["version"] == 1 and r["rule"] == 3)
        self.assertEqual((r3["suggested"], r3["accepted"], r3["rebutted"], r3["ignored"]),
                         (2, 1, 1, 0))
        r_none = next(r for r in rows if r["version"] == 1 and r["rule"] is None)
        self.assertEqual((r_none["suggested"], r_none["ignored"]), (1, 1))
        self.assertEqual(rows, sorted(rows, key=lambda r: (r["version"], (r["rule"] is None, r["rule"]))))

    def test_build_mode_rows_aggregates_per_version_mode(self):
        suggestions = [sugg("a", version=1, failure_mode="claim-drift"),
                      sugg("b", version=1, failure_mode="claim-drift"),
                      sugg("c", version=1, failure_mode=None)]
        outcomes = [
            {"suggestion_id": "a", "outcome": "accepted", "heuristics_version": 1, "failure_mode": "claim-drift"},
            {"suggestion_id": "b", "outcome": "rebutted", "heuristics_version": 1, "failure_mode": "claim-drift"},
            {"suggestion_id": "c", "outcome": "ignored", "heuristics_version": 1, "failure_mode": None},
        ]
        rows = build_mode_rows(suggestions, outcomes)
        r_cd = next(r for r in rows if r["version"] == 1 and r["failure_mode"] == "claim-drift")
        self.assertEqual((r_cd["suggested"], r_cd["accepted"], r_cd["rebutted"], r_cd["ignored"]),
                         (2, 1, 1, 0))
        r_none = next(r for r in rows if r["version"] == 1 and r["failure_mode"] is None)
        self.assertEqual((r_none["suggested"], r_none["ignored"]), (1, 1))
        self.assertEqual(rows, sorted(rows, key=lambda r: (r["version"],
                                                            (r["failure_mode"] is None, r["failure_mode"]))))


def _write_case(cases_dir: Path, name: str, expected: str, marker: str,
                expect_files: list[str] | None = None) -> None:
    cases_dir.mkdir(parents=True, exist_ok=True)
    (cases_dir / f"{name}.json").write_text(json.dumps({
        "name": name, "expected": expected, "expect_files": expect_files or [],
        "events": [{"type": "reasoning", "payload": {"text": marker}}],
        "latest_diff": None,
    }), encoding="utf-8")


def _make_stub(td: Path, script: str) -> Path:
    stub = td / "stub.py"
    stub.write_text(script)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


# Distinguishes calls by the marker build_prompt embeds — "HEURISTICS (vN):"
# comes from the heuristics text's own version header, so v1 = current, v2 =
# candidate without any extra plumbing.
ALWAYS_PASS_STUB = """#!/usr/bin/env python3
print("PASS")
"""

CANDIDATE_WORSE_STUB = """#!/usr/bin/env python3
import sys
text = open(sys.argv[1], encoding="utf-8").read()
if "HEURISTICS (v2)" in text:
    print('{"file": "a.py", "line": 1, "issue": "wrong", "severity": "medium"}')
else:
    print("PASS")
"""


class TestGateCandidate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cases_dir = Path(self.td.name) / "cases"

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def test_candidate_scores_lower_gate_fails(self):
        _write_case(self.cases_dir, "c1", "pass", "nothing risky")
        stub = _make_stub(Path(self.td.name), CANDIDATE_WORSE_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            ok, note = rewrite.gate_candidate("version: 2\n- candidate rule\n",
                                              "version: 1\n- current rule\n")
        self.assertFalse(ok)
        self.assertIn("candidate 0.00", note)
        self.assertIn("current 1.00", note)

    def test_equal_scores_gate_passes(self):
        _write_case(self.cases_dir, "c1", "pass", "nothing risky")
        stub = _make_stub(Path(self.td.name), ALWAYS_PASS_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            ok, note = rewrite.gate_candidate("version: 2\n- candidate rule\n",
                                              "version: 1\n- current rule\n")
        self.assertTrue(ok)
        self.assertIn("candidate 1.00", note)
        self.assertIn("current 1.00", note)

    def test_no_cases_dir_ungated(self):
        # self.cases_dir is never created — load_cases() sees an empty glob.
        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            ok, note = rewrite.gate_candidate("version: 2\n- x\n", "version: 1\n- y\n")
        self.assertTrue(ok)
        self.assertEqual(note, "no eval cases — ungated")


# The rewrite-prompt call and the two eval-scoring passes (candidate v2,
# current v1) are told apart purely by markers already present in the real
# prompts, so one stub script handles all three call shapes. Also logs one
# line per invocation next to itself, so tests can assert a second
# maybe_rewrite call made zero model calls (backoff after rejection).
REWRITE_GATE_FAIL_STUB = """#!/usr/bin/env python3
import os
import sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write("1\\n")
text = open(sys.argv[1], encoding="utf-8").read()
if "TASK: REWRITE HEURISTICS" in text:
    print("version: 2")
    print("- new rule")
elif "HEURISTICS (v2)" in text:
    print('{"file": "a.py", "line": 1, "issue": "wrong", "severity": "medium"}')
else:
    print("PASS")
"""

REWRITE_GATE_PASS_STUB = """#!/usr/bin/env python3
import sys
text = open(sys.argv[1], encoding="utf-8").read()
if "TASK: REWRITE HEURISTICS" in text:
    print("version: 2")
    print("- new rule")
elif "HEURISTICS (v1)" in text:
    print('{"file": "a.py", "line": 1, "issue": "wrong", "severity": "medium"}')
else:
    print("PASS")
"""


class TestMaybeRewriteGateIntegration(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()
        self.heuristics = self.cc / "heuristics.md"
        self.heuristics.write_text("version: 1\n- old rule\n")
        outcomes = self.cc / "outcomes.ndjsonl"
        with outcomes.open("w") as f:
            for outcome in ("accepted", "ignored", "rebutted"):
                f.write(json.dumps({"suggestion_id": outcome, "outcome": outcome,
                                    "issue": "x", "heuristics_version": 1}) + "\n")
        self.cases_dir = Path(self.td.name) / "cases"
        _write_case(self.cases_dir, "c1", "pass", "nothing risky")

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def test_gate_fail_rejects_and_leaves_heuristics_untouched(self):
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), REWRITE_GATE_FAIL_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            main_mod.maybe_rewrite(self.cc, {}, force=False)

        self.assertEqual(self.heuristics.read_text(), "version: 1\n- old rule\n")
        reflections = [json.loads(l) for l in
                       (self.cc / "reflections.ndjsonl").read_text().splitlines()]
        self.assertEqual(len(reflections), 1)
        self.assertEqual(reflections[0]["event"], "rewrite_rejected")
        self.assertEqual(reflections[0]["from_version"], 1)
        self.assertIn("candidate 0.00", reflections[0]["note"])
        self.assertFalse((self.cc / "heuristics-history").exists())

    def test_gate_pass_applies_and_records_gate_note(self):
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), REWRITE_GATE_PASS_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            main_mod.maybe_rewrite(self.cc, {}, force=False)

        self.assertEqual(self.heuristics.read_text(), "version: 2\n- new rule\n")
        reflections = [json.loads(l) for l in
                       (self.cc / "reflections.ndjsonl").read_text().splitlines()]
        self.assertEqual(len(reflections), 1)
        self.assertEqual(reflections[0]["from_version"], 1)
        self.assertEqual(reflections[0]["to_version"], 2)
        self.assertIn("candidate 1.00", reflections[0]["gate"])

    def test_gate_fail_backs_off_no_calls_on_next_pass_with_unchanged_outcomes(self):
        """A rejected candidate must not be regenerated + rescored every
        beat off the same stale outcomes — state has to advance so
        should_rewrite gates the next attempt until fresh grades land."""
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), REWRITE_GATE_FAIL_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        calls_log = Path(self.td.name) / "calls.log"
        state: dict = {}

        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            main_mod.maybe_rewrite(self.cc, state, force=False)
        self.assertTrue(calls_log.exists())
        first_pass_calls = len(calls_log.read_text().splitlines())
        self.assertGreater(first_pass_calls, 0)
        self.assertIn("n_graded_at_last_rewrite", state)

        with mock.patch("evals.run.CASES_DIR", self.cases_dir), \
             mock.patch("evals.run.HARVESTED_CASES_DIR", Path(self.td.name) / "no-harvested"):
            main_mod.maybe_rewrite(self.cc, state, force=False)  # same outcomes on disk

        self.assertEqual(len(calls_log.read_text().splitlines()), first_pass_calls,
                         "second pass made model calls despite no new graded outcomes")
        reflections = (self.cc / "reflections.ndjsonl").read_text().splitlines()
        self.assertEqual(len(reflections), 1)  # no second rewrite_rejected row


def _outcome(sid: str, outcome: str, version: int) -> dict:
    return {"suggestion_id": sid, "outcome": outcome, "issue": "x", "heuristics_version": version}


class TestMaybeRollback(unittest.TestCase):
    """v1 (archived) had 100% acceptance; v2 (current) underperforms at ~33%
    with >= MIN_NEW_OUTCOMES graded — auto-revert should kick in exactly
    once, restoring v1's rules under a new version number (v3)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()
        self.heuristics = self.cc / "heuristics.md"
        self.heuristics.write_text("version: 2\n- v2 rule\n")
        history = self.cc / "heuristics-history"
        history.mkdir()
        (history / "v1.md").write_text("version: 1\n- v1 rule\n")
        (self.cc / "suggestions.ndjsonl").write_text("")
        self.outcomes = [
            _outcome("a", "accepted", 1), _outcome("b", "accepted", 1),
            _outcome("c", "accepted", 2), _outcome("d", "ignored", 2), _outcome("e", "ignored", 2),
        ]

    def tearDown(self):
        self.td.cleanup()

    def test_reverts_once_then_no_ops(self):
        import reflector.main as main_mod

        state: dict = {}
        main_mod.maybe_rollback(self.cc, state, self.outcomes)

        self.assertEqual(self.heuristics.read_text(), "version: 3\n- v1 rule\n")
        self.assertEqual((self.cc / "heuristics-history" / "v2.md").read_text(),
                         "version: 2\n- v2 rule\n")
        reflections = [json.loads(l) for l in
                       (self.cc / "reflections.ndjsonl").read_text().splitlines()]
        self.assertEqual(len(reflections), 1)
        self.assertEqual(reflections[0]["event"], "rollback")
        self.assertEqual(reflections[0]["from_version"], 2)
        self.assertEqual(reflections[0]["restored_version_content_of"], 1)
        self.assertTrue(state["rolled_back_from_2"])

        # Second call: current version moved to 3, for which there's no
        # graded data at all, so this is a natural no-op too.
        before = self.heuristics.read_text()
        main_mod.maybe_rollback(self.cc, state, self.outcomes)
        self.assertEqual(self.heuristics.read_text(), before)
        self.assertEqual(len(
            (self.cc / "reflections.ndjsonl").read_text().splitlines()), 1)

    def test_rollback_advances_state_so_rewrite_does_not_thrash_same_pass(self):
        """Without advancing n_graded_at_last_rewrite on revert, a
        maybe_rewrite call right after maybe_rollback (same pass, same
        outcomes) would immediately try to rewrite the just-restored rules
        using the very outcomes that condemned the reverted-away version."""
        import reflector.main as main_mod

        state: dict = {}
        main_mod.maybe_rollback(self.cc, state, self.outcomes)
        self.assertEqual(self.heuristics.read_text(), "version: 3\n- v1 rule\n")

        total_graded = sum(1 for o in self.outcomes if o["outcome"] in rewrite.GRADED)
        self.assertEqual(state["n_graded_at_last_rewrite"], total_graded)
        self.assertFalse(rewrite.should_rewrite(
            self.outcomes, state["n_graded_at_last_rewrite"], force=False))

    def test_revert_once_guard_blocks_even_when_conditions_still_qualify(self):
        import reflector.main as main_mod

        state = {"rolled_back_from_2": True}
        main_mod.maybe_rollback(self.cc, state, self.outcomes)
        self.assertEqual(self.heuristics.read_text(), "version: 2\n- v2 rule\n")
        self.assertFalse((self.cc / "reflections.ndjsonl").exists())

    def test_missing_archive_is_a_noop(self):
        import reflector.main as main_mod

        (self.cc / "heuristics-history" / "v1.md").unlink()
        state: dict = {}
        main_mod.maybe_rollback(self.cc, state, self.outcomes)
        self.assertEqual(self.heuristics.read_text(), "version: 2\n- v2 rule\n")
        self.assertFalse((self.cc / "reflections.ndjsonl").exists())
        self.assertEqual(state, {})


ACCEPTED_GRADE_STUB = """#!/usr/bin/env python3
print('{"outcome": "accepted", "evidence": "diff added the guard"}')
"""


class TestGradePendingHarvestsCases(HarvestIsolatedTestCase):
    """Task 8: grade_pending must call reflector.harvest.maybe_harvest after
    appending each outcome row, so accepted/rebutted findings with case
    material grow the frozen eval set."""

    def setUp(self):
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()

        (self.cc / "suggestions.ndjsonl").write_text(json.dumps({
            "id": "s1", "ts": _iso(NOW - 400), "verdict": "SUGGESTION",
            "heuristics_version": 1,
            "suggestion": {"file": "a.py", "line": 3, "severity": "high",
                          "issue": "off-by-one", "rationale": "r"},
            "verification": {"status": "verified"},
        }) + "\n", encoding="utf-8")
        (self.cc / "delivered.json").write_text(
            json.dumps({"s1": {"context": NOW - 300}}), encoding="utf-8")
        material_dir = self.cc / "case-material"
        material_dir.mkdir()
        (material_dir / "s1.json").write_text(json.dumps({
            "events": [{"type": "diff", "payload": {"diff": "+guard"}}],
            "latest_diff": {"type": "diff", "payload": {"diff": "+guard"}},
        }), encoding="utf-8")

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def test_accepted_grade_harvests_a_flag_case(self):
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), ACCEPTED_GRADE_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        n = main_mod.grade_pending(self.cc)
        self.assertEqual(n, 1)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "accepted")

        case_path = self.harvested_dir / "harvest-s1.json"
        self.assertTrue(case_path.exists())
        case = json.loads(case_path.read_text())
        self.assertEqual(case["expected"], "flag")
        self.assertEqual(case["expect_files"], ["a.py"])

    def test_explicit_rebuttal_with_untouched_file_harvests_a_pass_case(self):
        import reflector.main as main_mod

        os.environ["CRITIC_CMD"] = "/nonexistent"  # rebuttal is model-free; must not be called
        (self.cc / "observations.ndjsonl").write_text(json.dumps({
            "ts": _iso(NOW - 250), "type": "reasoning",
            "payload": {"text": "COUNCIL-REBUTTAL: guard already exists on line 2"}},
        ) + "\n", encoding="utf-8")
        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "rebutted")
        self.assertFalse(outcomes[0]["file_touched"])

        case_path = self.harvested_dir / "harvest-s1.json"
        self.assertTrue(case_path.exists())
        self.assertEqual(json.loads(case_path.read_text())["expected"], "pass")

    def test_harvest_failure_does_not_kill_grade_pending(self):
        """Daemons never die on unexpected errors — a broken harvest.maybe_harvest
        (e.g. an OSError writing evals/cases-harvested/) must not take down
        grade_pending; the outcome row it already appended must survive."""
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), ACCEPTED_GRADE_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        with mock.patch("reflector.main.harvest.maybe_harvest",
                        side_effect=OSError("disk full")):
            n = main_mod.grade_pending(self.cc)
        self.assertEqual(n, 1)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["outcome"], "accepted")
        self.assertFalse((self.harvested_dir / "harvest-s1.json").exists())


# Only grades "accepted" when the prompt actually carries the fixed content —
# proves evidence() must surface untracked-file contents for grade_pending to
# credit a real fix that lives entirely in a new/untracked file.
FIX_MARKER_STUB = """#!/usr/bin/env python3
import sys
text = open(sys.argv[1], encoding="utf-8").read()
if "def fixed_safe_divide" in text:
    print('{"outcome": "accepted", "evidence": "untracked file now has the fix"}')
else:
    print('{"outcome": "ignored", "evidence": "no fix visible"}')
"""


class TestGradePendingCreditsUntrackedFix(HarvestIsolatedTestCase):
    """Fix 3: a real fix living entirely in an untracked file (never a tracked
    diff) must reach the grading model via evidence(), or it gets
    under-credited as 'ignored' — observed live."""

    def setUp(self):
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()

        (self.cc / "suggestions.ndjsonl").write_text(json.dumps({
            "id": "s1", "ts": _iso(NOW - 400), "verdict": "SUGGESTION",
            "heuristics_version": 1,
            "suggestion": {"file": "safe_math.py", "line": 3, "severity": "high",
                          "issue": "divide by zero", "rationale": "r"},
        }) + "\n", encoding="utf-8")
        (self.cc / "delivered.json").write_text(
            json.dumps({"s1": {"context": NOW - 300}}), encoding="utf-8")
        (self.cc / "observations.ndjsonl").write_text(json.dumps({
            "ts": _iso(NOW - 250), "type": "diff", "payload": {
                "diff": "",
                "untracked_contents": {
                    "safe_math.py": "def fixed_safe_divide(a, b):\n    return a / b if b else 0"},
            }}) + "\n", encoding="utf-8")

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def test_untracked_fix_is_accepted(self):
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), FIX_MARKER_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "accepted")


# Distinguishes TASK: GRADE from TASK: DISTILL calls by the marker each
# prompt starts with (both come from build_prompt/build_distill_prompt).
REBUT_AND_DISTILL_STUB = """#!/usr/bin/env python3
import sys
text = open(sys.argv[1], encoding="utf-8").read()
if "TASK: DISTILL" in text:
    print("Tests are stdlib unittest run via discover.")
elif "TASK: GRADE" in text:
    print('{"outcome": "rebutted", "evidence": "agent disagreed: coverage already exists"}')
else:
    print("PASS")
"""


class TestGradePendingDistillsKnowledge(HarvestIsolatedTestCase):
    """Task 5: a rebutted grade — on either the explicit-marker path or the
    model-judged path — distills into .codecouncil/knowledge.md so the same
    rebuttal never has to recur."""

    def setUp(self):
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()

        (self.cc / "suggestions.ndjsonl").write_text(json.dumps({
            "id": "s1", "ts": _iso(NOW - 400), "verdict": "SUGGESTION",
            "heuristics_version": 1,
            "suggestion": {"file": "a.py", "line": 3, "severity": "medium",
                          "issue": "missing test coverage", "rationale": "r"},
        }) + "\n", encoding="utf-8")
        (self.cc / "delivered.json").write_text(
            json.dumps({"s1": {"context": NOW - 300}}), encoding="utf-8")

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def test_rebutted_grade_distills_a_fact(self):
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), REBUT_AND_DISTILL_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "rebutted")
        self.assertIn("stdlib unittest", (self.cc / "knowledge.md").read_text())

    def test_explicit_rebuttal_also_distills_a_fact(self):
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), REBUT_AND_DISTILL_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        (self.cc / "observations.ndjsonl").write_text(json.dumps({
            "ts": _iso(NOW - 250), "type": "reasoning",
            "payload": {"text": "COUNCIL-REBUTTAL: coverage already exists in b_test.py"}},
        ) + "\n", encoding="utf-8")

        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "rebutted")
        self.assertIn("stdlib unittest", (self.cc / "knowledge.md").read_text())

    def test_distill_failure_never_kills_grading(self):
        """A distill call that raises must not take down grade_pending — the
        outcome row it already appended must survive."""
        import reflector.main as main_mod

        stub = _make_stub(Path(self.td.name), REBUT_AND_DISTILL_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        real_ask = main_mod._ask

        def flaky_ask(prompt_text):
            if prompt_text.startswith("TASK: DISTILL"):
                raise RuntimeError("model unreachable")
            return real_ask(prompt_text)

        with mock.patch("reflector.main._ask", side_effect=flaky_ask):
            n = main_mod.grade_pending(self.cc)

        self.assertEqual(n, 1)
        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["outcome"], "rebutted")
        self.assertFalse((self.cc / "knowledge.md").exists())


class TestOutcomeRowsCarryRule(HarvestIsolatedTestCase):
    """Task 6: outcome rows copy "rule" from the suggestion being graded, on
    both the explicit-rebuttal and model-judged paths — so every grade
    traces back to the heuristic that caused it. "missed"/"undelivered"
    outcomes never had a suggestion dict to copy from, so they stay without
    a "rule" field entirely (not even null).

    Task 1: "failure_mode" rides along the same two paths and the same
    missed/undelivered exclusion — mirrored fixtures below."""

    def setUp(self):
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def _write_suggestion(self, rule, failure_mode=None):
        (self.cc / "suggestions.ndjsonl").write_text(json.dumps({
            "id": "s1", "ts": _iso(NOW - 400), "verdict": "SUGGESTION",
            "heuristics_version": 1,
            "suggestion": {"file": "a.py", "line": 3, "severity": "high",
                          "issue": "bug", "rationale": "r", "rule": rule,
                          "failure_mode": failure_mode},
        }) + "\n", encoding="utf-8")
        (self.cc / "delivered.json").write_text(
            json.dumps({"s1": {"context": NOW - 300}}), encoding="utf-8")

    def test_explicit_rebuttal_path_copies_rule(self):
        import reflector.main as main_mod

        self._write_suggestion(rule=2, failure_mode="claim-drift")
        (self.cc / "observations.ndjsonl").write_text(json.dumps({
            "ts": _iso(NOW - 250), "type": "reasoning",
            "payload": {"text": "COUNCIL-REBUTTAL: not applicable here"}},
        ) + "\n", encoding="utf-8")
        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "rebutted")
        self.assertEqual(outcomes[0]["rule"], 2)
        self.assertEqual(outcomes[0]["failure_mode"], "claim-drift")

    def test_model_judged_path_copies_null_rule(self):
        import reflector.main as main_mod

        self._write_suggestion(rule=None, failure_mode=None)
        stub = _make_stub(Path(self.td.name), ACCEPTED_GRADE_STUB)
        os.environ["CRITIC_CMD"] = str(stub)
        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "accepted")
        self.assertIn("rule", outcomes[0])
        self.assertIsNone(outcomes[0]["rule"])
        self.assertIn("failure_mode", outcomes[0])
        self.assertIsNone(outcomes[0]["failure_mode"])

    def test_undelivered_outcome_has_no_rule_field(self):
        import reflector.main as main_mod

        (self.cc / "suggestions.ndjsonl").write_text(json.dumps({
            "id": "s1", "ts": _iso(NOW - 1000), "verdict": "SUGGESTION",
            "heuristics_version": 1,
            "suggestion": {"file": "a.py", "line": 3, "severity": "high",
                          "issue": "bug", "rationale": "r", "rule": 2,
                          "failure_mode": "claim-drift"},
        }) + "\n", encoding="utf-8")
        main_mod.grade_pending(self.cc)

        outcomes = [json.loads(l) for l in
                   (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outcomes[0]["outcome"], "undelivered")
        self.assertNotIn("rule", outcomes[0])
        self.assertNotIn("failure_mode", outcomes[0])


class TestMissedGrading(HarvestIsolatedTestCase):
    """Task 3: grade_pending must also detect PASS verdicts later contradicted
    by a fix commit (reflector.misses), grade them 'missed' with no model
    call, and harvest them as must-flag eval cases."""

    def setUp(self):
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"
        self.cc.mkdir()

        self.pass_ts = NOW - 3000
        (self.cc / "suggestions.ndjsonl").write_text(json.dumps({
            "id": "p1", "ts": _iso(self.pass_ts), "verdict": "PASS",
            "heuristics_version": 2, "reviewed_files": ["a.py"],
            "reason": "looked fine",
        }) + "\n", encoding="utf-8")
        (self.cc / "observations.ndjsonl").write_text(json.dumps({
            "type": "commit", "ts": _iso(self.pass_ts + 100),
            "payload": {"subjects": ["abc123 fix off-by-one in a.py"],
                       "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"},
        }) + "\n", encoding="utf-8")
        material_dir = self.cc / "case-material"
        material_dir.mkdir()
        (material_dir / "p1.json").write_text(json.dumps({
            "events": [{"type": "diff", "payload": {"diff": "+guard"}}],
            "latest_diff": None,
        }), encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_missed_pass_gets_graded_and_harvested(self):
        import reflector.main as main_mod

        n = main_mod.grade_pending(self.cc)
        self.assertEqual(n, 1)

        outc = [json.loads(l) for l in
               (self.cc / "outcomes.ndjsonl").read_text().splitlines()]
        self.assertEqual(outc[-1]["outcome"], "missed")
        self.assertEqual(outc[-1]["suggestion_id"], "p1")
        self.assertEqual(outc[-1]["heuristics_version"], 2)
        self.assertTrue(any(p.name.startswith("harvest-") for p in
                            self.harvested_dir.glob("*.json")))

    def test_missed_grade_is_idempotent_across_passes(self):
        import reflector.main as main_mod

        main_mod.grade_pending(self.cc)
        before = len((self.cc / "outcomes.ndjsonl").read_text().splitlines())
        main_mod.grade_pending(self.cc)
        after = len((self.cc / "outcomes.ndjsonl").read_text().splitlines())
        self.assertEqual(before, after)

    def test_miss_detection_failure_does_not_kill_grade_pending(self):
        """Daemons never die: a broken misses.detect_misses must not take
        down grade_pending or lose outcomes already appended this pass."""
        import reflector.main as main_mod

        with mock.patch("reflector.main.misses.detect_misses",
                        side_effect=RuntimeError("boom")):
            main_mod.grade_pending(self.cc)  # must not raise
        self.assertFalse((self.cc / "outcomes.ndjsonl").exists())


if __name__ == "__main__":
    unittest.main()
