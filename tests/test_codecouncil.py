"""Tests for the codecouncil launcher's preflight warnings."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codecouncil import main as launcher


class TestPreflight(unittest.TestCase):
    def test_warns_when_pi_missing(self):
        with mock.patch.object(launcher.shutil, "which", return_value=None):
            with mock.patch.object(launcher.agent, "_local_env", return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None)
        self.assertTrue(any("not found on PATH" in w for w in warns))

    def test_warns_when_no_model_and_no_key(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env", return_value={}):
                warns = launcher.preflight(model=None)
        self.assertTrue(any("no model configured" in w for w in warns))

    def test_clean_when_pi_present_and_key_set(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env", return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None)
        self.assertEqual(warns, [])

    def test_clean_when_model_passed_explicitly(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env", return_value={}):
                warns = launcher.preflight(model="openai/gpt-4o")
        self.assertEqual(warns, [])


class TestPreflightProber(unittest.TestCase):
    """Task 4: council mode's --prober is a second model call — an
    openrouter/* prober with no OPENROUTER_API_KEY would fail every beat,
    same failure shape the existing model/key checks above already guard
    against, so it gets the same preflight treatment."""

    def test_warns_when_prober_openrouter_and_no_key(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env",
                                   return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None, prober="openrouter/openai/gpt-5-mini")
        self.assertTrue(any("OPENROUTER_API_KEY" in w for w in warns))

    def test_no_warning_when_prober_key_present(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env",
                                   return_value={"NVIDIA_API_KEY": "x",
                                                 "OPENROUTER_API_KEY": "y"}):
                warns = launcher.preflight(model=None, prober="openrouter/openai/gpt-5-mini")
        self.assertEqual(warns, [])

    def test_no_warning_when_prober_none(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env",
                                   return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None, prober=None)
        self.assertEqual(warns, [])

    def test_no_warning_when_prober_not_openrouter(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(launcher.agent, "_local_env",
                                   return_value={"NVIDIA_API_KEY": "x"}):
                warns = launcher.preflight(model=None, prober="nvidia-nim/nvidia/nemotron")
        self.assertEqual(warns, [])

    def test_prober_from_env_when_no_flag(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/bin/pi"):
            with mock.patch.object(
                launcher.agent, "_local_env",
                return_value={"NVIDIA_API_KEY": "x",
                              "COUNCIL_PROBER": "openrouter/openai/gpt-5-mini"}):
                warns = launcher.preflight(model=None, prober=None)
        self.assertTrue(any("OPENROUTER_API_KEY" in w for w in warns))


if __name__ == "__main__":
    unittest.main()
