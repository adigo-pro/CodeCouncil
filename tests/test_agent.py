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
                    env = agent.local_env()
            self.assertEqual(env["FOO_KEY"], "from-file")
            self.assertEqual(env["BAR"], "also-from-file")

    def test_real_env_wins_over_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "env"
            f.write_text("FOO_KEY=from-file\n")
            with mock.patch.object(agent, "LOCAL_ENV_FILE", f):
                with mock.patch.dict(os.environ, {"FOO_KEY": "from-real-env"}):
                    env = agent.local_env()
            self.assertEqual(env["FOO_KEY"], "from-real-env")

    def test_missing_file_is_fine(self):
        with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
            env = agent.local_env()
        self.assertIsInstance(env, dict)

    def test_default_model_only_when_unset_and_key_present(self):
        with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
            with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "x"}, clear=False):
                os.environ.pop("COUNCIL_MODEL", None)
                env = agent.local_env()
                model = env.get("COUNCIL_MODEL") or (
                    agent.DEFAULT_NVIDIA_MODEL if env.get("NVIDIA_API_KEY") else None
                )
            self.assertEqual(model, agent.DEFAULT_NVIDIA_MODEL)


class TestAskCommandConstruction(unittest.TestCase):
    """Task 4: judgment turns pass tools=/cwd= through to the real `pi`
    invocation. No real pi call — a PI_BIN shim stands in and records what it
    was given."""

    def setUp(self):
        self._saved_critic_cmd = os.environ.pop("CRITIC_CMD", None)

    def tearDown(self):
        if self._saved_critic_cmd is not None:
            os.environ["CRITIC_CMD"] = self._saved_critic_cmd
        else:
            os.environ.pop("CRITIC_CMD", None)

    def test_ask_with_tools_and_cwd_builds_readonly_flags(self):
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td) / "work"
            work_dir.mkdir()
            argv_file = Path(td) / "argv.txt"
            pwd_file = Path(td) / "pwd.txt"
            stub = Path(td) / "pi_stub.sh"
            stub.write_text(
                "#!/bin/sh\n"
                f'pwd > "{pwd_file}"\n'
                f'printf \'%s\\n\' "$@" > "{argv_file}"\n'
                "echo ok\n"
            )
            stub.chmod(stub.stat().st_mode | 0o755)

            with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
                with mock.patch.dict(os.environ, {"PI_BIN": str(stub)}, clear=False):
                    os.environ.pop("COUNCIL_MODEL", None)
                    os.environ.pop("NVIDIA_API_KEY", None)
                    agent.ask("investigate this", tools="repo_read,repo_grep,repo_find,repo_ls",
                              cwd=str(work_dir))

            argv = argv_file.read_text().splitlines()
            self.assertIn("--tools", argv)
            tools_idx = argv.index("--tools")
            self.assertEqual(argv[tools_idx + 1], "repo_read,repo_grep,repo_find,repo_ls")
            self.assertNotIn("bash", argv[tools_idx + 1].split(","))
            self.assertEqual(pwd_file.read_text().strip(), str(work_dir.resolve()))

            # Both extensions attached — the jailed repo_* tools alongside
            # the (unrelated) NVIDIA provider extension. Order isn't asserted
            # since agent.py's own attach order is an implementation detail;
            # what matters is both paths are present as --extension values.
            extension_flags = [argv[i + 1] for i, a in enumerate(argv) if a == "--extension"]
            self.assertEqual(argv.count("--extension"), 2)
            self.assertIn(str(agent.NVIDIA_EXTENSION), extension_flags)
            self.assertIn(str(agent.REPO_TOOLS_EXTENSION), extension_flags)

    def test_ask_model_param_overrides_council_model_env(self):
        """Task 1: explicit model= wins over COUNCIL_MODEL — this is how the
        council selects a prober from a worker thread without mutating
        os.environ (which would race the main thread)."""
        with tempfile.TemporaryDirectory() as td:
            argv_file = Path(td) / "argv.txt"
            stub = Path(td) / "pi_stub.sh"
            stub.write_text(
                "#!/bin/sh\n"
                f'printf \'%s\\n\' "$@" > "{argv_file}"\n'
                "echo ok\n"
            )
            stub.chmod(stub.stat().st_mode | 0o755)

            with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
                with mock.patch.dict(os.environ, {"PI_BIN": str(stub),
                                                    "COUNCIL_MODEL": "openai/gpt-4o"},
                                      clear=False):
                    agent.ask("investigate this", model="openrouter/openai/gpt-5-mini")

            argv = argv_file.read_text().splitlines()
            self.assertIn("--model", argv)
            model_idx = argv.index("--model")
            self.assertEqual(argv[model_idx + 1], "openrouter/openai/gpt-5-mini")
            # exactly one --model flag — the param, not the env var
            self.assertEqual(argv.count("--model"), 1)

    def test_ask_model_none_keeps_council_model_resolution(self):
        """model=None (the default) must not disturb the existing
        COUNCIL_MODEL/NVIDIA-default resolution."""
        with tempfile.TemporaryDirectory() as td:
            argv_file = Path(td) / "argv.txt"
            stub = Path(td) / "pi_stub.sh"
            stub.write_text(
                "#!/bin/sh\n"
                f'printf \'%s\\n\' "$@" > "{argv_file}"\n'
                "echo ok\n"
            )
            stub.chmod(stub.stat().st_mode | 0o755)

            with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
                with mock.patch.dict(os.environ, {"PI_BIN": str(stub),
                                                    "COUNCIL_MODEL": "openai/gpt-4o"},
                                      clear=False):
                    agent.ask("investigate this")

            argv = argv_file.read_text().splitlines()
            self.assertIn("--model", argv)
            model_idx = argv.index("--model")
            self.assertEqual(argv[model_idx + 1], "openai/gpt-4o")


class TestAskStubModelArgv(unittest.TestCase):
    """Task 1: the CRITIC_CMD stub invocation gains the resolved model as a
    second argv so multi-model tests (council mode) can answer per-model.
    Existing single-arg stubs (`#!/bin/sh\\necho ...`) ignore extra argv, so
    this is zero-breakage for them."""

    def setUp(self):
        self._saved_critic_cmd = os.environ.pop("CRITIC_CMD", None)
        self._saved_council_model = os.environ.pop("COUNCIL_MODEL", None)

    def tearDown(self):
        if self._saved_critic_cmd is not None:
            os.environ["CRITIC_CMD"] = self._saved_critic_cmd
        else:
            os.environ.pop("CRITIC_CMD", None)
        if self._saved_council_model is not None:
            os.environ["COUNCIL_MODEL"] = self._saved_council_model
        else:
            os.environ.pop("COUNCIL_MODEL", None)

    def test_stub_receives_explicit_model_as_second_argv(self):
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "stub.sh"
            stub.write_text("#!/bin/sh\necho \"got:$2\"\n")
            stub.chmod(stub.stat().st_mode | 0o755)
            os.environ["CRITIC_CMD"] = str(stub)

            reply = agent.ask("hello", model="openrouter/openai/gpt-5-mini")

            self.assertEqual(reply, "got:openrouter/openai/gpt-5-mini")

    def test_stub_receives_council_model_env_as_second_argv_when_no_param(self):
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "stub.sh"
            stub.write_text("#!/bin/sh\necho \"got:$2\"\n")
            stub.chmod(stub.stat().st_mode | 0o755)
            os.environ["CRITIC_CMD"] = str(stub)
            os.environ["COUNCIL_MODEL"] = "openai/gpt-4o"

            reply = agent.ask("hello")

            self.assertEqual(reply, "got:openai/gpt-4o")

    def test_stub_receives_empty_second_argv_when_no_model_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "stub.sh"
            stub.write_text("#!/bin/sh\necho \"got:[$2]\"\n")
            stub.chmod(stub.stat().st_mode | 0o755)
            os.environ["CRITIC_CMD"] = str(stub)

            with mock.patch.object(agent, "LOCAL_ENV_FILE", Path("/nonexistent/env")):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("NVIDIA_API_KEY", None)
                    reply = agent.ask("hello")

            self.assertEqual(reply, "got:[]")


class TestDefaultModelOrder(unittest.TestCase):
    """Any single API key must yield a working model with zero /model step."""

    def test_each_key_alone_picks_its_default(self):
        from core import config as cfg
        from critic.agent import _resolve_model
        for key, default in cfg.KEY_DEFAULT_MODELS:
            self.assertEqual(_resolve_model(None, {key: "x"}), default, key)

    def test_nvidia_wins_when_multiple_keys(self):
        from critic.agent import DEFAULT_NVIDIA_MODEL, _resolve_model
        env = {"OPENAI_API_KEY": "x", "NVIDIA_API_KEY": "x", "ANTHROPIC_API_KEY": "x"}
        self.assertEqual(_resolve_model(None, env), DEFAULT_NVIDIA_MODEL)

    def test_explicit_and_env_still_win(self):
        from critic.agent import _resolve_model
        env = {"NVIDIA_API_KEY": "x", "COUNCIL_MODEL": "openai/gpt-5-mini"}
        self.assertEqual(_resolve_model(None, env), "openai/gpt-5-mini")
        self.assertEqual(_resolve_model("groq/openai/gpt-oss-120b", env),
                         "groq/openai/gpt-oss-120b")

    def test_no_keys_resolves_none(self):
        from critic.agent import _resolve_model
        self.assertIsNone(_resolve_model(None, {}))


if __name__ == "__main__":
    unittest.main()
