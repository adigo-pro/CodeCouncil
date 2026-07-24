"""A/B eval harness: the pure scoring pieces."""

import unittest

from evals.ab import score
from evals.ab.tasks import TASKS


class TestParseChecks(unittest.TestCase):
    def test_parses_pass_and_fail_lines(self):
        out = "noise\nCHECK a PASS\nCHECK b-c FAIL\ntrailing"
        self.assertEqual(score.parse_checks(out), {"a": True, "b-c": False})

    def test_no_checks_is_empty(self):
        self.assertEqual(score.parse_checks("Traceback (most recent call last)"), {})


class TestTestsRun(unittest.TestCase):
    def test_detects_unittest_and_pytest(self):
        self.assertTrue(score.tests_run(["python3 -m unittest discover"]))
        self.assertTrue(score.tests_run(["pytest -q"]))
        self.assertFalse(score.tests_run(["git commit -m 'tested and ready'"]))


class TestTaskDefinitions(unittest.TestCase):
    def test_every_task_is_well_formed(self):
        names = set()
        for name, category, instruction, hidden in TASKS:
            names.add(name)
            self.assertIn(category, {"trap", "hard", "clean", "claim"})
            self.assertTrue(instruction.strip().endswith(("Commit.", "commit.",
                                                          "this time.", "message.")))
            self.assertIn("CHECK", hidden)
            compile(hidden, name, "exec")  # hidden tests must at least parse
        self.assertEqual(len(names), len(TASKS), "task names must be unique")


if __name__ == "__main__":
    unittest.main()
