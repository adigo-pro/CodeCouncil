"""A/B eval harness: the pure scoring pieces."""

import io
import json
import os
import sys
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

    def test_training_run_task_also_passes_isolation_flag(self):
        # training sessions generate the data driving harvested cases + rewrites;
        # they need the same contamination guard as the A/B harness.
        import training.run as training_run
        with mock.patch.object(training_run, "sh") as sh_mock:
            sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            training_run.run_task(Path("/tmp/repo"), "do it.")
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


class TestGateWiring(unittest.TestCase):
    """Task 2 (run-3): --gate SECONDS (default 90, raised from 45 — a
    verified finding was measured landing ~9s after the old 45s cap gave
    up) turns the done-gate on for the with-council arm only, by threading
    COUNCIL_GATE_SECONDS into the claude subprocess's own environment (not
    the harness's os.environ)."""

    def test_argparse_default_is_90(self):
        self.assertEqual(ab_run.build_parser().parse_args([]).gate, 90)

    def test_argparse_flag_parses(self):
        self.assertEqual(ab_run.build_parser().parse_args(["--gate", "10"]).gate, 10)

    def _claude_env(self, arm: str, gate: int = 45):
        """Runs run_trial for the given arm+gate and returns the env dict
        (or None) passed to the claude sh() call. Daemons/sleeps are mocked
        out so a 'with'-arm trial doesn't actually wait ~28s."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(ab_run, "install_hooks"), \
                 mock.patch.object(ab_run.subprocess, "Popen") as popen, \
                 mock.patch.object(ab_run, "sh") as sh_mock, \
                 mock.patch.object(ab_run.time, "sleep"), \
                 mock.patch.object(ab_run.score, "run_hidden_test",
                                    return_value={"passed": 0, "total": 0, "all_pass": False,
                                                  "checks": {}, "crashed": False, "output": ""}), \
                 mock.patch.object(ab_run.score, "git_facts",
                                    return_value={"commits": 0, "last_subject": ""}), \
                 mock.patch.object(ab_run.score, "council_stats",
                                    return_value={"findings": 0, "passes": 0,
                                                  "receipts": 0, "delivered": 0}), \
                 mock.patch.object(ab_run, "find_project_dir", return_value=None):
                popen.return_value = mock.Mock()
                sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                ab_run.run_trial(base, "task", "clean", "do the thing. Commit.",
                                 "CHECK x PASS", arm, 1, gate=gate)
            claude_calls = [c for c in sh_mock.call_args_list if c.args[0][0] == "claude"]
            self.assertEqual(len(claude_calls), 1)
            return claude_calls[0].kwargs.get("env")

    def test_with_arm_env_carries_gate_seconds(self):
        env = self._claude_env("with", gate=45)
        self.assertIsNotNone(env)
        self.assertEqual(env.get("COUNCIL_GATE_SECONDS"), "45")

    def test_without_arm_env_has_no_gate_var(self):
        env = self._claude_env("without", gate=45)
        self.assertTrue(env is None or "COUNCIL_GATE_SECONDS" not in env)

    def test_naive_arm_env_has_no_gate_var(self):
        env = self._claude_env("naive", gate=45)
        self.assertTrue(env is None or "COUNCIL_GATE_SECONDS" not in env)

    def test_gate_zero_disables_for_with_arm(self):
        env = self._claude_env("with", gate=0)
        self.assertTrue(env is None or "COUNCIL_GATE_SECONDS" not in env)


class TestProberWiring(unittest.TestCase):
    """Task 2 (run-3): --prober MODEL turns council mode on for the
    with-council arm's critic spawn (critic/main.py already accepts
    --prober). Default is the measured high-recall prober
    (openrouter/openai/gpt-5-mini); 'off'/'none' disables; without/naive
    never see it because they never spawn a critic at all."""

    def test_argparse_default_is_gpt5mini(self):
        args = ab_run.build_parser().parse_args([])
        self.assertEqual(args.prober, "openrouter/openai/gpt-5-mini")

    def test_argparse_off_disables(self):
        self.assertIsNone(ab_run.build_parser().parse_args(["--prober", "off"]).prober)

    def test_argparse_none_disables(self):
        self.assertIsNone(ab_run.build_parser().parse_args(["--prober", "none"]).prober)

    def test_argparse_custom_model(self):
        args = ab_run.build_parser().parse_args(["--prober", "some/model"])
        self.assertEqual(args.prober, "some/model")

    def _critic_argv(self, prober):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.object(ab_run, "install_hooks"), \
                 mock.patch.object(ab_run.subprocess, "Popen") as popen:
                popen.return_value = mock.Mock()
                ab_run.start_council(repo, prober=prober)
            self.assertEqual(popen.call_count, 2)
            return popen.call_args_list[1].args[0]

    def test_start_council_includes_prober_by_default(self):
        argv = self._critic_argv("openrouter/openai/gpt-5-mini")
        self.assertIn("--prober", argv)
        self.assertEqual(argv[argv.index("--prober") + 1], "openrouter/openai/gpt-5-mini")

    def test_start_council_includes_custom_prober(self):
        argv = self._critic_argv("some/model")
        self.assertIn("--prober", argv)
        self.assertEqual(argv[argv.index("--prober") + 1], "some/model")

    def test_start_council_omits_prober_when_off(self):
        argv = self._critic_argv(None)
        self.assertNotIn("--prober", argv)

    def _run_trial_argv(self, arm: str, prober):
        """without/naive never call start_council at all (no daemons), so
        --prober can never appear anywhere in their path regardless of the
        harness-level --prober setting."""
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
                                 "CHECK x PASS", arm, 1, prober=prober)
            return popen.call_count

    def test_without_arm_never_spawns_critic(self):
        self.assertEqual(
            self._run_trial_argv("without", "openrouter/openai/gpt-5-mini"), 0)

    def test_naive_arm_never_spawns_critic(self):
        self.assertEqual(
            self._run_trial_argv("naive", "openrouter/openai/gpt-5-mini"), 0)


class TestProberKeyWarning(unittest.TestCase):
    """Task 2 (run-3): missing OPENROUTER_API_KEY is a warning, never a
    crash — mirrors codecouncil/main.py's preflight() warning style."""

    def test_warns_when_prober_on_and_key_absent(self):
        msg = ab_run.prober_key_warning("openrouter/openai/gpt-5-mini", {})
        self.assertIsNotNone(msg)
        self.assertIn("OPENROUTER_API_KEY", msg)

    def test_no_warning_when_key_present(self):
        msg = ab_run.prober_key_warning(
            "openrouter/openai/gpt-5-mini", {"OPENROUTER_API_KEY": "sk-x"})
        self.assertIsNone(msg)

    def test_no_warning_when_prober_off(self):
        self.assertIsNone(ab_run.prober_key_warning(None, {}))

    def test_no_warning_for_non_openrouter_prober(self):
        self.assertIsNone(ab_run.prober_key_warning("nvidia-nim/some/model", {}))

    def test_main_prints_warning_to_stderr_when_with_arm_and_key_absent(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ab_run, "install_hooks"), \
             mock.patch.object(ab_run.subprocess, "Popen") as popen, \
             mock.patch.object(ab_run, "sh") as sh_mock, \
             mock.patch.object(ab_run.time, "sleep"), \
             mock.patch.object(ab_run.score, "run_hidden_test",
                                return_value={"passed": 0, "total": 0, "all_pass": False,
                                              "checks": {}, "crashed": False, "output": ""}), \
             mock.patch.object(ab_run.score, "git_facts",
                                return_value={"commits": 0, "last_subject": ""}), \
             mock.patch.object(ab_run.score, "run_adversarial_test",
                                return_value={"safe": True}), \
             mock.patch.object(ab_run.score, "council_stats",
                                return_value={"findings": 0, "passes": 0,
                                              "receipts": 0, "delivered": 0}), \
             mock.patch.object(ab_run, "find_project_dir", return_value=None), \
             mock.patch.object(ab_run.agent, "local_env", return_value={}):
            popen.return_value = mock.Mock()
            sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            buf = io.StringIO()
            with mock.patch.object(sys, "stderr", buf):
                ab_run.main(["--tier", "safety", "--tasks", "1", "--arms", "with",
                            "--out", tmp])
        self.assertIn("OPENROUTER_API_KEY", buf.getvalue())

    def test_main_no_warning_when_key_present(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ab_run, "install_hooks"), \
             mock.patch.object(ab_run.subprocess, "Popen") as popen, \
             mock.patch.object(ab_run, "sh") as sh_mock, \
             mock.patch.object(ab_run.time, "sleep"), \
             mock.patch.object(ab_run.score, "run_hidden_test",
                                return_value={"passed": 0, "total": 0, "all_pass": False,
                                              "checks": {}, "crashed": False, "output": ""}), \
             mock.patch.object(ab_run.score, "git_facts",
                                return_value={"commits": 0, "last_subject": ""}), \
             mock.patch.object(ab_run.score, "run_adversarial_test",
                                return_value={"safe": True}), \
             mock.patch.object(ab_run.score, "council_stats",
                                return_value={"findings": 0, "passes": 0,
                                              "receipts": 0, "delivered": 0}), \
             mock.patch.object(ab_run, "find_project_dir", return_value=None), \
             mock.patch.object(ab_run.agent, "local_env",
                                return_value={"OPENROUTER_API_KEY": "sk-x"}):
            popen.return_value = mock.Mock()
            sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            buf = io.StringIO()
            with mock.patch.object(sys, "stderr", buf):
                ab_run.main(["--tier", "safety", "--tasks", "1", "--arms", "with",
                            "--out", tmp])
        self.assertNotIn("OPENROUTER_API_KEY", buf.getvalue())


class TestCouncilStatsDelivered(unittest.TestCase):
    """Task 2: council_stats()['delivered'] counts real suggestion ids in
    delivered.json, excluding the reserved channel keys."""

    def test_delivered_counts_non_reserved_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cc = repo / ".codecouncil"
            cc.mkdir()
            (cc / "delivered.json").write_text(json.dumps({
                "abc123def456": {"context": 1.0},
                "fed654cba321": {"block": 2.0},
                "receipts": {"r1.md": 1.0},
                "gate": {"sess-1": 1.0},
            }), encoding="utf-8")
            stats = score.council_stats(repo)
            self.assertEqual(stats["delivered"], 2)

    def test_missing_ledger_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".codecouncil").mkdir()
            stats = score.council_stats(repo)
            self.assertEqual(stats["delivered"], 0)

    def test_corrupt_ledger_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cc = repo / ".codecouncil"
            cc.mkdir()
            (cc / "delivered.json").write_text("not json", encoding="utf-8")
            stats = score.council_stats(repo)
            self.assertEqual(stats["delivered"], 0)


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

    def test_crashed_hidden_scores_zero_not_excluded_from_mean(self):
        # Two trials for one arm: a clean 2/2 and a crash. The crash must pull
        # the mean to 50%, not be dropped (which would report 100%).
        rows = [
            {"task": "t", "arm": "without", "hidden": {"passed": 2, "total": 2},
             "tests_run": True, "session": {"rc": 0}},
            {"task": "t", "arm": "without",
             "hidden": {"passed": 0, "total": 0, "crashed": True, "output": "boom"},
             "tests_run": False, "session": {"rc": 1}},
        ]
        md = ab_run.report(rows)
        self.assertIn("50% over 2 trials", md)
        self.assertIn("1 crashed → scored 0", md)

    def test_error_row_helpers_have_report_compatible_shape(self):
        feat = ab_run._error_feature_row("t", "claim", "with", 1, "setup blew up")
        saf = ab_run._error_safety_row("doc-reader", "without", 1, "setup blew up")
        # both must flow through report() without KeyError, and score as failures
        md = ab_run.report([feat, saf])
        self.assertIn("crash", md)
        self.assertIn("UNSAFE", md)
        self.assertTrue(feat["hidden"]["crashed"])
        self.assertFalse(saf["safe"])


class TestRepoUrlParsing(unittest.TestCase):
    """Task 5: --repo-url URL@sha, opt-in real-OSS-repo substrate."""

    def test_parses_url_and_sha(self):
        args = ab_run.build_parser().parse_args(
            ["--repo-url", "https://github.com/tiangolo/full-stack-fastapi-template@abc123"])
        self.assertEqual(
            args.repo_url,
            ("https://github.com/tiangolo/full-stack-fastapi-template", "abc123"))

    def test_missing_sha_is_a_parser_error(self):
        with self.assertRaises(SystemExit):
            ab_run.build_parser().parse_args(
                ["--repo-url", "https://github.com/tiangolo/full-stack-fastapi-template"])

    def test_default_is_none(self):
        self.assertIsNone(ab_run.build_parser().parse_args([]).repo_url)

    def test_ssh_style_url_with_embedded_at_still_splits_on_the_last_one(self):
        args = ab_run.build_parser().parse_args(
            ["--repo-url", "git@github.com:tiangolo/full-stack-fastapi-template@abc123"])
        self.assertEqual(
            args.repo_url,
            ("git@github.com:tiangolo/full-stack-fastapi-template", "abc123"))


class TestRepoUrlSubstrate(unittest.TestCase):
    """run_trial (feature tier): --repo-url set clones+pins instead of
    seeding synthetic files; unset stays on today's seed_repo path with no
    network touched at all."""

    def _run(self, repo_url):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(ab_run, "seed_repo") as seed_mock, \
                 mock.patch.object(ab_run, "sh") as sh_mock, \
                 mock.patch.object(ab_run.score, "run_hidden_test",
                                    return_value={"passed": 0, "total": 0, "all_pass": False,
                                                  "checks": {}, "crashed": False, "output": ""}), \
                 mock.patch.object(ab_run.score, "git_facts",
                                    return_value={"commits": 0, "last_subject": ""}), \
                 mock.patch.object(ab_run, "find_project_dir", return_value=None):
                sha = repo_url[1] if repo_url else ""

                def fake_sh(cmd, *a, **k):
                    # clone_repo verifies the checkout took via `git rev-parse
                    # HEAD` == sha; return the pinned sha for that call.
                    out = sha if cmd[:2] == ["git", "rev-parse"] else ""
                    return mock.Mock(returncode=0, stdout=out, stderr="")

                sh_mock.side_effect = fake_sh
                ab_run.run_trial(base, "task", "clean", "do the thing. Commit.",
                                 "CHECK x PASS", "without", 1, repo_url=repo_url)
            git_argvs = [c.args[0] for c in sh_mock.call_args_list
                        if c.args[0] and c.args[0][0] == "git"]
            return seed_mock, git_argvs

    def test_repo_url_set_clones_and_checks_out_pinned_sha_no_seed_repo(self):
        seed_mock, git_argvs = self._run(("https://github.com/x/y", "abc123"))
        seed_mock.assert_not_called()
        clone_argv = next(a for a in git_argvs if a[1] == "clone")
        self.assertIn("https://github.com/x/y", clone_argv)
        self.assertIn("--depth", clone_argv)
        # the pinned sha shows up somewhere in the fetch/checkout sequence
        self.assertTrue(any("abc123" in a for a in git_argvs if a is not clone_argv))

    def test_repo_url_unset_seeds_synthetic_with_no_clone_command(self):
        seed_mock, git_argvs = self._run(None)
        seed_mock.assert_called_once()
        self.assertFalse(any(a[1] == "clone" for a in git_argvs))


class TestMethodologyCommandsParse(unittest.TestCase):
    """Task 6: docs/benchmarks/METHODOLOGY.md's reproduce commands must
    parse via the real build_parser() — a documented flag that stops
    existing should fail CI, not mislead a reader."""

    def test_selftest_flag_parses(self):
        args = ab_run.build_parser().parse_args(["--selftest"])
        self.assertTrue(args.selftest)

    def test_full_run_command_parses(self):
        args = ab_run.build_parser().parse_args(
            ["--tier", "both", "--arms", "all", "--trials", "4"])
        self.assertEqual(args.tier, "both")
        self.assertEqual(args.arms, ["without", "naive", "with"])
        self.assertEqual(args.trials, 4)

    def test_real_repo_run_command_parses(self):
        args = ab_run.build_parser().parse_args([
            "--tier", "feature", "--arms", "all", "--trials", "4",
            "--repo-url",
            "https://github.com/tiangolo/full-stack-fastapi-template@abc123",
        ])
        self.assertEqual(args.tier, "feature")
        self.assertEqual(
            args.repo_url,
            ("https://github.com/tiangolo/full-stack-fastapi-template", "abc123"))

    def test_rescore_module_is_importable(self):
        # documented as `python3 -m evals.ab.rescore <run-dir>` — importing
        # confirms the module still exists under that name.
        from evals.ab import rescore  # noqa: F401


class TestRepoUrlIgnoredBySafetyTier(unittest.TestCase):
    """Safety-tier trials always use their own per-task seed_files; a real
    repo has no bearing on a surgical single-function safety scenario, so
    --repo-url must never reach the safety path even when both are set."""

    def test_safety_tier_never_clones_even_with_repo_url_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ab_run, "install_hooks"), \
                 mock.patch.object(ab_run.subprocess, "Popen"), \
                 mock.patch.object(ab_run, "sh") as sh_mock, \
                 mock.patch.object(ab_run, "clone_repo") as clone_mock, \
                 mock.patch.object(ab_run.score, "run_adversarial_test",
                                    return_value={"safe": True}), \
                 mock.patch.object(ab_run.score, "git_facts",
                                    return_value={"commits": 0, "last_subject": ""}), \
                 mock.patch.object(ab_run, "find_project_dir", return_value=None):
                sh_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                ab_run.main(["--tier", "safety", "--tasks", "1", "--arms", "without",
                            "--out", tmp,
                            "--repo-url",
                            "https://github.com/tiangolo/full-stack-fastapi-template@abc123"])
            clone_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
