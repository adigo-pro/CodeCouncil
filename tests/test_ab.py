"""A/B eval harness: the pure scoring pieces."""

import json
import os
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


class TestArmsParsing(unittest.TestCase):
    """Task 1: --arms accepts a comma list, 'all', and the 'both' back-compat alias."""

    def test_comma_list(self):
        args = ab_run.build_parser().parse_args(["--arms", "without,naive,with"])
        self.assertEqual(args.arms, ["without", "naive", "with"])

    def test_all_expands_to_three_arms(self):
        args = ab_run.build_parser().parse_args(["--arms", "all"])
        self.assertEqual(args.arms, ["without", "naive", "with"])

    def test_both_is_backward_compatible(self):
        args = ab_run.build_parser().parse_args(["--arms", "both"])
        self.assertEqual(args.arms, ["without", "with"])

    def test_default_is_both(self):
        args = ab_run.build_parser().parse_args([])
        self.assertEqual(args.arms, ["without", "with"])

    def test_single_arm(self):
        args = ab_run.build_parser().parse_args(["--arms", "naive"])
        self.assertEqual(args.arms, ["naive"])

    def test_unknown_arm_rejected(self):
        with self.assertRaises(SystemExit):
            ab_run.build_parser().parse_args(["--arms", "bogus"])


class TestNaiveArm(unittest.TestCase):
    """Task 1: naive arm is isolated like 'without' but appends the self-review nudge."""

    def _run_trial(self, arm: str):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(ab_run, "install_hooks"), \
                 mock.patch.object(ab_run.subprocess, "Popen") as popen, \
                 mock.patch.object(ab_run, "sh") as sh_mock, \
                 mock.patch.object(ab_run.score, "run_hidden_test",
                                    return_value={"passed": 0, "total": 0, "all_pass": False,
                                                  "checks": {}, "crashed": False, "output": ""}), \
                 mock.patch.object(ab_run.score, "git_facts",
                                    return_value={"commits": 0, "last_subject": ""}), \
                 mock.patch.object(ab_run, "find_project_dir", return_value=None):
                sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                ab_run.run_trial(base, "task", "clean", "do the thing. Commit.",
                                 "CHECK x PASS", arm, 1)
            claude_calls = [c for c in sh_mock.call_args_list if c.args[0][0] == "claude"]
            self.assertEqual(len(claude_calls), 1)
            return popen.call_count, claude_calls[0].args[0]

    def test_naive_starts_no_daemons_and_appends_nudge(self):
        popen_count, argv = self._run_trial("naive")
        self.assertEqual(popen_count, 0)
        self.assertIn("--append-system-prompt", argv)
        self.assertIn(ab_run.NAIVE_REVIEW_PROMPT, argv)

    def test_without_starts_no_daemons_and_has_no_append(self):
        popen_count, argv = self._run_trial("without")
        self.assertEqual(popen_count, 0)
        self.assertNotIn("--append-system-prompt", argv)


class TestSettingSourcesIsolation(unittest.TestCase):
    """Task 3: every claude invocation excludes global/user Claude settings so
    a council hook sitting in ~/.claude/settings.json can never reach a
    benchmark session, regardless of arm (the ponytail contamination
    lesson — see run.py's module docstring)."""

    def test_run_session_passes_isolation_flag(self):
        with mock.patch.object(ab_run, "sh") as sh_mock:
            sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ab_run.run_session(Path("/tmp/repo"), "do it.")
        argv = sh_mock.call_args.args[0]
        self.assertIn("--setting-sources", argv)
        sources = argv[argv.index("--setting-sources") + 1].split(",")
        self.assertNotIn("user", sources)
        self.assertNotIn("global", sources)

    def test_with_arm_session_still_loads_project_settings(self):
        # The 'with' arm's treatment is the installed project hooks + daemons
        # (start_council -> install_hooks writes .claude/settings.json in the
        # repo), so its claude invocation must still load 'project'.
        with mock.patch.object(ab_run, "sh") as sh_mock:
            sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ab_run.run_session(Path("/tmp/repo"), "do it.")
        argv = sh_mock.call_args.args[0]
        sources = argv[argv.index("--setting-sources") + 1].split(",")
        self.assertIn("project", sources)

    def test_isolated_arm_excludes_planted_global_marker_hook(self):
        """Integration-flavored: plant a marker hook in a fake global settings
        dir (simulating a maintainer's machine with council hooks installed
        globally), run a 'without'-arm trial, and assert the constructed
        claude argv's --setting-sources would exclude that global hook. This
        is exactly the ponytail failure mode: a SessionStart hook reachable
        from every arm including the untreated baseline."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "fake-home"
            claude_dir = fake_home / ".claude"
            claude_dir.mkdir(parents=True)
            (claude_dir / "settings.json").write_text(
                json.dumps({"hooks": {"SessionStart": [{"hooks": [
                    {"type": "command", "command": "echo CONTAMINATION_MARKER"}
                ]}]}}),
                encoding="utf-8")

            base = Path(tmp) / "base"
            with mock.patch.object(ab_run, "install_hooks"), \
                 mock.patch.object(ab_run.subprocess, "Popen"), \
                 mock.patch.object(ab_run, "sh") as sh_mock, \
                 mock.patch.object(ab_run.score, "run_hidden_test",
                                    return_value={"passed": 0, "total": 0, "all_pass": False,
                                                  "checks": {}, "crashed": False, "output": ""}), \
                 mock.patch.object(ab_run.score, "git_facts",
                                    return_value={"commits": 0, "last_subject": ""}), \
                 mock.patch.object(ab_run, "find_project_dir", return_value=None), \
                 mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                ab_run.run_trial(base, "task", "clean", "do the thing. Commit.",
                                 "CHECK x PASS", "without", 1)
            claude_calls = [c for c in sh_mock.call_args_list if c.args[0][0] == "claude"]
            self.assertEqual(len(claude_calls), 1)
            argv = claude_calls[0].args[0]
            sources = argv[argv.index("--setting-sources") + 1].split(",")
            self.assertNotIn("user", sources)
            self.assertNotIn("global", sources)


class TestReportThreeArms(unittest.TestCase):
    """Task 1: report() generalizes to whichever arms are actually present in rows."""

    def test_report_emits_a_mean_line_per_present_arm(self):
        rows = [
            {"task": "t", "arm": "without", "hidden": {"passed": 1, "total": 2},
             "tests_run": True, "session": {"rc": 0}},
            {"task": "t", "arm": "naive", "hidden": {"passed": 2, "total": 2},
             "tests_run": True, "session": {"rc": 0}},
            {"task": "t", "arm": "with", "hidden": {"passed": 2, "total": 2},
             "tests_run": True, "session": {"rc": 0}},
        ]
        md = ab_run.report(rows)
        self.assertIn("without", md)
        self.assertIn("naive", md)
        self.assertIn("with", md)
        # one mean-summary line per arm
        self.assertEqual(md.count("mean hidden-test pass rate"), 3)


if __name__ == "__main__":
    unittest.main()
