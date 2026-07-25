"""A/B eval harness: the pure scoring pieces."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.ab import run as ab_run
from evals.ab import score
from evals.ab.tasks import TASKS

TASK_CATEGORIES = {"trap", "hard", "clean", "claim", "security", "logic", "deps", "gaming"}


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
            self.assertIn(category, TASK_CATEGORIES)
            self.assertTrue(instruction.strip().endswith(("Commit.", "commit.",
                                                          "this time.", "message.")))
            self.assertIn("CHECK", hidden)
            compile(hidden, name, "exec")  # hidden tests must at least parse
        self.assertEqual(len(names), len(TASKS), "task names must be unique")


class TestProbesWiring(unittest.TestCase):
    """Task 6: --probes is opt-in and only reaches the critic spawn when set."""

    def test_argparse_default_is_off(self):
        self.assertFalse(ab_run.build_parser().parse_args([]).probes)

    def test_argparse_flag_turns_it_on(self):
        self.assertTrue(ab_run.build_parser().parse_args(["--probes"]).probes)

    def _critic_argv(self, probes: bool) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.object(ab_run, "install_hooks"), \
                 mock.patch.object(ab_run.subprocess, "Popen") as popen:
                popen.return_value = mock.Mock()
                ab_run.start_council(repo, probes=probes)
            # spawn() is called for observer first, then critic
            self.assertEqual(popen.call_count, 2)
            return popen.call_args_list[1].args[0]

    def test_start_council_includes_probes_when_set(self):
        self.assertIn("--probes", self._critic_argv(True))

    def test_start_council_omits_probes_by_default(self):
        self.assertNotIn("--probes", self._critic_argv(False))


if __name__ == "__main__":
    unittest.main()
