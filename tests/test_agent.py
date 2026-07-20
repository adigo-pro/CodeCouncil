"""Tests for critic/agent.py's local credential loading — never touches the
real ~/.codecouncil/env, and never asserts on any real secret value."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critic import agent


class TestLocalEnv(unittest.TestCase):
    def test_file_fills_in_unset_vars(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "env"
            f.write_text("FOO_KEY=from-file\n# comment\n\nBAR=also-from-file\n")
            with mock.patch.object(agent, "LOCAL_ENV_FILE", f):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("FOO_KEY", None)
                    os.environ.pop("BAR", None)
                    env = agent._local_env()
            self.assertEqual(env["FOO_KEY"], "from-file")
            self.assertEqual(env["BAR"], "also-from-file")

    def test_real_env_wins_over_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "env"
            f.write_text("FOO_KEY=from-file\n")
            with mock.patch.object(agent, "LOCAL_ENV_FILE", f):
                with mock.patch.dict(os.environ, {"FOO_KEY": "from-real-env"}):
                    env = agent._local_env()
            self.assertEqual(env["FOO_KEY"], "from-real-env")

    def test_missing_file_is_fine(self):
        with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
            env = agent._local_env()
        self.assertIsInstance(env, dict)

    def test_default_model_only_when_unset_and_key_present(self):
        with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
            with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "x"}, clear=False):
                os.environ.pop("COUNCIL_MODEL", None)
                env = agent._local_env()
                model = env.get("COUNCIL_MODEL") or (
                    agent.DEFAULT_NVIDIA_MODEL if env.get("NVIDIA_API_KEY") else None
                )
            self.assertEqual(model, agent.DEFAULT_NVIDIA_MODEL)


if __name__ == "__main__":
    unittest.main()
