"""SAFETY tier: discrimination proof (good ref -> SAFE, bad ref -> UNSAFE),
task schema, tier selection, and safe-rate aggregation.

Feature-tier / harness-wiring tests stay in tests/test_ab.py — this file is
safety-tier only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evals.ab import run as ab_run
from evals.ab import score
from evals.ab.safety_tasks import SAFETY_TASKS


def materialize(files: dict[str, str], root: Path) -> None:
    for path, content in files.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


class TestDiscrimination(unittest.TestCase):
    """The heart of Task 2: each adversarial_test must pass its task's good
    reference and fail its bad reference, run directly — no API calls."""

    def test_good_ref_is_safe_and_bad_ref_is_unsafe(self):
        for task in SAFETY_TASKS:
            with self.subTest(task=task.name, ref="good"), \
                    tempfile.TemporaryDirectory() as tmp:
                materialize(task.good, Path(tmp))
                result = score.run_adversarial_test(Path(tmp), task.adversarial_test)
                self.assertTrue(result["safe"],
                                 f"{task.name} good ref flagged UNSAFE: {result['output']}")
            with self.subTest(task=task.name, ref="bad"), \
                    tempfile.TemporaryDirectory() as tmp:
                materialize(task.bad, Path(tmp))
                result = score.run_adversarial_test(Path(tmp), task.adversarial_test)
                self.assertFalse(result["safe"],
                                  f"{task.name} bad ref flagged SAFE: {result['output']}")


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


if __name__ == "__main__":
    unittest.main()
