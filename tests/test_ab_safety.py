"""SAFETY tier: discrimination proof (good ref -> SAFE, bad ref -> UNSAFE),
task schema, tier selection, and safe-rate aggregation.

Feature-tier / harness-wiring tests stay in tests/test_ab.py — this file is
safety-tier only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.ab import run as ab_run
from evals.ab import score
from evals.ab.safety_tasks import SafetyTask, SAFETY_TASKS

# Reused by both the discrimination test below and evals.ab.run.selftest_safety
# — one copy of "write a file tree into a tempdir", not two.
materialize = ab_run.materialize


class TestDiscrimination(unittest.TestCase):
    """The heart of Task 2: each adversarial_test must pass its task's good
    reference and fail its bad reference, run directly — no API calls. Task
    4 wraps this exact check as evals.ab.run.selftest_safety(); reuse it here
    instead of re-running the good/bad loop by hand, so the discrimination
    proof and the --selftest gate can never drift apart."""

    def test_good_ref_is_safe_and_bad_ref_is_unsafe(self):
        all_ok, lines = ab_run.selftest_safety()
        self.assertTrue(all_ok, f"scorer(s) failed to discriminate: {lines}")
        self.assertEqual(len(lines), len(SAFETY_TASKS))
        for task in SAFETY_TASKS:
            self.assertIn(f"{task.name}: DISCRIMINATES", lines)


class TestTaskSchema(unittest.TestCase):
    def test_every_task_has_required_fields_and_compiles(self):
        names = set()
        for task in SAFETY_TASKS:
            names.add(task.name)
            self.assertTrue(task.name)
            self.assertTrue(task.instruction.strip())
            self.assertNotIn("inject", task.instruction.lower())
            self.assertNotIn("traversal", task.instruction.lower())
            self.assertNotIn("sql injection", task.instruction.lower())
            self.assertIn("SAFE", task.adversarial_test)
            self.assertIn("sys.exit", task.adversarial_test)
            self.assertTrue(task.seed_files)
            self.assertTrue(task.good)
            self.assertTrue(task.bad)
            for files in (task.seed_files, task.good, task.bad):
                for path, content in files.items():
                    compile(content, f"{task.name}/{path}", "exec")
            compile(task.adversarial_test, f"{task.name}-adversarial", "exec")
        self.assertEqual(len(names), len(SAFETY_TASKS), "task names must be unique")
        self.assertEqual(len(SAFETY_TASKS), 5)


class TestTierSelector(unittest.TestCase):
    def test_default_tier_is_feature(self):
        args = ab_run.build_parser().parse_args([])
        self.assertEqual(args.tier, "feature")

    def test_tier_accepts_safety_and_both(self):
        self.assertEqual(ab_run.build_parser().parse_args(["--tier", "safety"]).tier, "safety")
        self.assertEqual(ab_run.build_parser().parse_args(["--tier", "both"]).tier, "both")

    def test_tier_rejects_unknown_value(self):
        with self.assertRaises(SystemExit):
            ab_run.build_parser().parse_args(["--tier", "bogus"])


class TestSafetyTierSeeding(unittest.TestCase):
    """The safety tier must seed each task's OWN seed_files, not the shared
    training.run.SEED_FILES used by the feature tier."""

    def test_seed_repo_writes_given_files_not_shared_seed_files(self):
        task = SAFETY_TASKS[0]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            ab_run.seed_repo(repo, task.seed_files)
            for path, content in task.seed_files.items():
                self.assertEqual((repo / path).read_text(encoding="utf-8"), content)
            # none of the shared feature-tier seed files should have leaked in
            from training.run import SEED_FILES
            for shared_name in SEED_FILES:
                if shared_name not in task.seed_files:
                    self.assertFalse((repo / shared_name).exists())


class TestReportSafetyTier(unittest.TestCase):
    def test_report_shows_safe_rate_line_when_safety_rows_present(self):
        rows = [
            {"task": "doc-reader", "arm": "without", "safe": False,
             "tests_run": False, "session": {"rc": 0}},
            {"task": "doc-reader", "arm": "with", "safe": True,
             "tests_run": False, "session": {"rc": 0}},
        ]
        md = ab_run.report(rows)
        self.assertIn("safe-rate", md)
        self.assertIn("UNSAFE", md)

    def test_report_omits_safe_rate_when_no_safety_rows(self):
        rows = [{"task": "t", "arm": "without", "hidden": {"passed": 1, "total": 1},
                 "tests_run": True, "session": {"rc": 0}}]
        self.assertNotIn("safe-rate", ab_run.report(rows))


class TestSafeRate(unittest.TestCase):
    def test_aggregates_per_arm(self):
        rows = [
            {"arm": "without", "safe": True},
            {"arm": "without", "safe": False},
            {"arm": "with", "safe": True},
            {"arm": "with", "safe": True},
            {"task": "x", "arm": "with", "hidden": {"passed": 1, "total": 1}},  # no 'safe' key
        ]
        self.assertEqual(score.safe_rate(rows), {"without": (1, 2), "with": (2, 2)})

    def test_empty_rows(self):
        self.assertEqual(score.safe_rate([]), {})


class TestSelftest(unittest.TestCase):
    """Task 4: `--selftest` proves every safety scorer discriminates good
    from bad BEFORE any API spend. Test 3 (test_selftest_safety_catches_a_
    broken_scorer) is the important one — a scorer that can't tell good
    from bad must fail the gate, not silently pass."""

    def test_selftest_flag_parses_and_defaults_off(self):
        self.assertFalse(ab_run.build_parser().parse_args([]).selftest)
        self.assertTrue(ab_run.build_parser().parse_args(["--selftest"]).selftest)

    def test_selftest_safety_discriminates_on_shipped_tasks(self):
        ok, lines = ab_run.selftest_safety()
        self.assertTrue(ok, lines)
        self.assertEqual(len(lines), len(SAFETY_TASKS))
        self.assertTrue(all("DISCRIMINATES" in line for line in lines))

    def test_selftest_safety_catches_a_broken_scorer(self):
        # adversarial_test always reports SAFE regardless of which reference
        # is under test — good passes (fine) but bad also passes (BROKEN):
        # a scorer that can't tell good from bad must not report clean.
        broken = SafetyTask(
            name="broken-always-safe",
            seed_files={"m.py": "x = 1\n"},
            instruction="irrelevant to the self-test",
            adversarial_test="import sys\nprint('SAFE')\nsys.exit(0)\n",
            good={"m.py": "x = 1\n"},
            bad={"m.py": "x = 1\n"},
        )
        with mock.patch.object(ab_run, "SAFETY_TASKS", [broken]):
            ok, lines = ab_run.selftest_safety()
            self.assertFalse(ok)
            self.assertIn("BROKEN", lines[0])
            self.assertIn("bad=SAFE", lines[0])

            rc = ab_run.main(["--selftest"])
        self.assertNotEqual(rc, 0)

    def test_main_selftest_returns_zero_and_spawns_no_sessions(self):
        # selftest_safety() DOES use subprocess (to execute each adversarial
        # test locally, scoring only) — that's not what's being asserted
        # here. What must never happen is a coding-agent session or a
        # council daemon, so assert the higher-level entry points for those
        # are never reached.
        with mock.patch.object(ab_run, "start_council") as start_council, \
                mock.patch.object(ab_run, "run_session") as run_session:
            rc = ab_run.main(["--selftest"])
        self.assertEqual(rc, 0)
        start_council.assert_not_called()
        run_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
