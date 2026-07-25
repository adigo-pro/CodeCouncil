"""Tests for the codecouncil launcher's preflight warnings."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codecouncil import main as launcher


class TestPreflight(unittest.TestCase):
    def test_warns_when_pi_missing(self):
        with mock.patch.object(launcher.shutil, "which", return_value=None):
            with mock.patch.object(launcher.agent, "local_env", return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None)
        self.assertTrue(any("not found on PATH" in w for w in warns))

    def test_warns_when_no_model_and_no_key(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env", return_value={}):
                warns = launcher.preflight(model=None)
        self.assertTrue(any("no model configured" in w for w in warns))

    def test_clean_when_pi_present_and_key_set(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env", return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None)
        self.assertEqual(warns, [])

    def test_clean_when_model_passed_explicitly(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env", return_value={}):
                warns = launcher.preflight(model="openai/gpt-4o")
        self.assertEqual(warns, [])


class TestPreflightProber(unittest.TestCase):
    """Task 4: council mode's --prober is a second model call — an
    openrouter/* prober with no OPENROUTER_API_KEY would fail every beat,
    same failure shape the existing model/key checks above already guard
    against, so it gets the same preflight treatment."""

    def test_warns_when_prober_openrouter_and_no_key(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env",
                                   return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None, prober="openrouter/openai/gpt-5-mini")
        self.assertTrue(any("OPENROUTER_API_KEY" in w for w in warns))

    def test_no_warning_when_prober_key_present(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env",
                                   return_value={"NVIDIA_API_KEY": "x",
                                                 "OPENROUTER_API_KEY": "y"}):
                warns = launcher.preflight(model=None, prober="openrouter/openai/gpt-5-mini")
        self.assertEqual(warns, [])

    def test_no_warning_when_prober_none(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env",
                                   return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None, prober=None)
        self.assertEqual(warns, [])

    def test_no_warning_when_prober_not_openrouter(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "local_env",
                                   return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None, prober="nvidia-nim/nvidia/nemotron")
        self.assertEqual(warns, [])

    def test_prober_from_env_when_no_flag(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(
                launcher.agent, "local_env",
                return_value={"NVIDIA_API_KEY": "x",
                              "COUNCIL_PROBER": "openrouter/openai/gpt-5-mini"}):
                warns = launcher.preflight(model=None, prober=None)
        self.assertTrue(any("OPENROUTER_API_KEY" in w for w in warns))




class TestConfigModule(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.addCleanup(self.td.cleanup)

    def test_save_and_load_roundtrip_merge_delete(self):
        from core import config as cfg
        cfg.save_config({"model": "a/b"}, base=self.base)
        cfg.save_config({"prober": "c/d"}, base=self.base)
        self.assertEqual(cfg.load_config(self.base), {"model": "a/b", "prober": "c/d"})
        cfg.save_config({"prober": None}, base=self.base)
        self.assertEqual(cfg.load_config(self.base), {"model": "a/b"})

    def test_corrupt_config_ignored(self):
        from core import config as cfg
        cfg.config_path(self.base).parent.mkdir(parents=True, exist_ok=True)
        cfg.config_path(self.base).write_text("{nope")
        self.assertEqual(cfg.load_config(self.base), {})

    def test_update_env_key_replaces_preserves_comments_mode(self):
        from core import config as cfg
        import os as _os
        cfg.env_path(self.base).parent.mkdir(parents=True, exist_ok=True)
        cfg.env_path(self.base).write_text("# comment\nNVIDIA_API_KEY=old\nOTHER=x\n")
        cfg.update_env_key("NVIDIA_API_KEY", "new", base=self.base)
        text = cfg.env_path(self.base).read_text()
        self.assertIn("# comment", text)
        self.assertIn("NVIDIA_API_KEY=new", text)
        self.assertNotIn("old", text)
        self.assertIn("OTHER=x", text)
        self.assertEqual(_os.stat(cfg.env_path(self.base)).st_mode & 0o777, 0o600)

    def test_resolve_precedence(self):
        from core import config as cfg
        cfg.save_config({"model": "from-config"}, base=self.base)
        self.assertEqual(cfg.resolve("flag", "M", "model", {"M": "from-env"}, self.base), "flag")
        self.assertEqual(cfg.resolve(None, "M", "model", {"M": "from-env"}, self.base), "from-env")
        self.assertEqual(cfg.resolve(None, "M", "model", {}, self.base), "from-config")
        self.assertIsNone(cfg.resolve(None, "M", "nope", {}, self.base))


class TestConsoleParsing(unittest.TestCase):
    def setUp(self):
        # Console commands persist via core.config's default CONFIG_DIR —
        # tests must NEVER touch the real ~/.codecouncil (a /model test once
        # silently rewrote the live config). Point the module at a tempdir.
        import tempfile
        from core import config as cfg
        self.td = tempfile.TemporaryDirectory()
        self._orig = cfg.CONFIG_DIR
        cfg.CONFIG_DIR = Path(self.td.name)
        self.addCleanup(lambda: setattr(cfg, "CONFIG_DIR", self._orig))
        self.addCleanup(self.td.cleanup)

    def test_parse_command_shapes(self):
        from codecouncil.console import parse_command
        self.assertEqual(parse_command("/model a/b"), ("model", "a/b"))
        self.assertEqual(parse_command("/help"), ("help", ""))
        self.assertEqual(parse_command("  /PROBER off "), ("prober", "off"))
        self.assertIsNone(parse_command("not a command"))
        self.assertIsNone(parse_command("/"))

    def test_unknown_command_and_exception_never_raise(self):
        from codecouncil.console import Console
        msgs = []
        c = Console(repo=Path("."), restart_critic=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                    stop=lambda: None, say=msgs.append)
        c.handle("/nope")
        self.assertTrue(any("unknown" in m for m in msgs))
        c.handle("/model x/y")  # restart_critic raises; handle must swallow
        self.assertTrue(any("failed" in m for m in msgs))




class TestSignalFilter(unittest.TestCase):
    def test_idle_chatter_drops(self):
        from codecouncil.signal_filter import classify, DROP
        for line in [
            "· beat 123 · 10:00:00 · nothing new, no call made",
            "· beat 5 · 09:12:01 · 3 event(s) held — no code change yet",
            "· beat 9 · 09:12:01 · cooling down — 2 event(s) queued",
            "reflector: nothing to grade",
        ]:
            self.assertEqual(classify(line), DROP, line)

    def test_signal_moments_highlight(self):
        from codecouncil.signal_filter import classify, HIGHLIGHT
        for line in [
            "■ beat 42 · 10:00:00 · MEDIUM · payments.py:7",
            "  verify verified: ZeroDivisionError raised",
            "reflector: abc123 → accepted (fixed it)",
            "reflector: abc123 → distilled fact: tests run via discover",
            "reflector: heuristics v3 → v4 (archived v3.md)",
            "reflector: auto-rollback v4 → v5 (restoring rules of v3, archived v4.md)",
            "critic: receipt written to /x/y.md",
            "⚠ beat 9 · 10:00:00 · malformed reply — treated as PASS; raw: x",
        ]:
            self.assertEqual(classify(line), HIGHLIGHT, line)

    def test_narration_stays_normal_and_ansi_stripped(self):
        from codecouncil.signal_filter import classify, NORMAL, DROP
        self.assertEqual(classify("♥ beat 12 · 10:00:00 · 3 event(s)"), NORMAL)
        self.assertEqual(classify("✓ beat 12 · 10:00:00 · PASS — clean"), NORMAL)
        # daemons write plain text to pipes, but tolerate ANSI anyway
        self.assertEqual(classify("\x1b[2m· beat 1 · 10:00:00 · nothing new, no call made\x1b[0m"), DROP)


if __name__ == "__main__":
    unittest.main()
