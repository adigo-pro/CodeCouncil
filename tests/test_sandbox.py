"""core/sandbox.py — the OS boundary around code CodeCouncil did not write.

The pure builders are asserted on directly; the end-to-end class actually
executes the demonstrated exploit through `critic.probe.run_script` and
requires it to fail. That last part is the test that matters: this module
exists because an env-only mitigation was believed sufficient and wasn't, so
the regression guard has to run the real attack, not assert on a config.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import sandbox  # noqa: E402
from critic.probe import run_script  # noqa: E402


class TestResolvePolicy(unittest.TestCase):
    def test_defaults_to_auto(self):
        self.assertEqual(sandbox.resolve_policy(None, None), sandbox.POLICY_AUTO)

    def test_env_wins_over_config(self):
        self.assertEqual(sandbox.resolve_policy("off", "require"), sandbox.POLICY_OFF)

    def test_config_used_when_env_absent(self):
        self.assertEqual(sandbox.resolve_policy(None, "require"), sandbox.POLICY_REQUIRE)

    def test_case_and_whitespace_tolerant(self):
        self.assertEqual(sandbox.resolve_policy("  REQUIRE ", None), sandbox.POLICY_REQUIRE)

    def test_unknown_value_falls_back_to_auto(self):
        self.assertEqual(sandbox.resolve_policy("banana", None), sandbox.POLICY_AUTO)

    def test_non_string_config_ignored(self):
        self.assertEqual(sandbox.resolve_policy(None, 7), sandbox.POLICY_AUTO)


class TestMacosProfile(unittest.TestCase):
    def test_denies_network_and_home(self):
        p = sandbox.macos_profile("/tmp/staging", "/Users/x", [])
        self.assertIn("(deny network*)", p)
        self.assertIn('(deny file-read* (subpath "/Users/x"))', p)

    def test_interpreter_reallow_comes_after_home_denial(self):
        """SBPL applies the LAST matching rule. pyenv/asdf put the interpreter
        under the home directory, so if the re-allow preceded the denial,
        Python itself would be unreadable and every script would fail."""
        p = sandbox.macos_profile("/tmp/staging", "/Users/x", ["/Users/x/.pyenv/versions/3.12.3"])
        deny_at = p.index('(deny file-read* (subpath "/Users/x"))')
        allow_at = p.index('(allow file-read* (subpath "/Users/x/.pyenv/versions/3.12.3"))')
        self.assertLess(deny_at, allow_at)

    def test_staging_writable_and_last(self):
        p = sandbox.macos_profile("/tmp/staging", "/Users/x", ["/Users/x/.pyenv"])
        self.assertIn('(allow file-read* file-write* (subpath "/tmp/staging"))', p)
        self.assertTrue(p.rstrip().endswith('(subpath "/tmp/staging"))'))

    def test_quotes_in_path_escaped(self):
        """An unescaped quote would terminate the SBPL string early and could
        change the meaning of the profile."""
        p = sandbox.macos_profile('/tmp/st"age', "/Users/x", [])
        self.assertIn(r'/tmp/st\"age', p)


class TestBwrapArgv(unittest.TestCase):
    def test_unshares_network(self):
        argv = sandbox.bwrap_argv("/tmp/staging", "/home/x", [])
        self.assertIn("--unshare-net", argv)

    def test_tmpfs_over_home_then_interpreter_rebound(self):
        argv = sandbox.bwrap_argv("/tmp/staging", "/home/x", ["/home/x/.pyenv"])
        joined = " ".join(argv)
        self.assertIn("--tmpfs /home/x", joined)
        # order matters: bwrap applies binds sequentially, so the interpreter
        # re-bind must land on top of the tmpfs that hid the home
        self.assertLess(joined.index("--tmpfs /home/x"),
                        joined.index("--ro-bind-try /home/x/.pyenv"))

    def test_staging_bound_writable(self):
        argv = sandbox.bwrap_argv("/tmp/staging", "/home/x", [])
        self.assertIn("--bind", argv)
        self.assertIn("/tmp/staging", argv)


class TestWrap(unittest.TestCase):
    def test_policy_off_is_a_passthrough(self):
        argv, sandboxed = sandbox.wrap(["python3", "x.py"], "/tmp/s", sandbox.POLICY_OFF)
        self.assertEqual(argv, ["python3", "x.py"])
        self.assertFalse(sandboxed)

    def test_require_raises_when_no_mechanism(self):
        real = sandbox.mechanism
        sandbox.mechanism = lambda: None
        try:
            with self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.wrap(["python3"], "/tmp/s", sandbox.POLICY_REQUIRE)
        finally:
            sandbox.mechanism = real

    def test_auto_degrades_to_unsandboxed_when_no_mechanism(self):
        real = sandbox.mechanism
        sandbox.mechanism = lambda: None
        try:
            argv, sandboxed = sandbox.wrap(["python3"], "/tmp/s", sandbox.POLICY_AUTO)
            self.assertEqual(argv, ["python3"])
            self.assertFalse(sandboxed)
        finally:
            sandbox.mechanism = real

    def test_wraps_when_mechanism_present(self):
        if sandbox.mechanism() is None:
            self.skipTest("no sandbox mechanism on this host")
        argv, sandboxed = sandbox.wrap(["python3", "x.py"], "/tmp/s", sandbox.POLICY_AUTO)
        self.assertTrue(sandboxed)
        self.assertNotEqual(argv[0], "python3")
        self.assertEqual(argv[-2:], ["python3", "x.py"])


class TestMinimalEnv(unittest.TestCase):
    def test_no_secrets_pass_through(self):
        os.environ["NVIDIA_API_KEY"] = "nvapi-should-never-propagate"
        try:
            env = sandbox.minimal_env(home="/tmp/staging")
            self.assertNotIn("NVIDIA_API_KEY", env)
            self.assertNotIn("nvapi-should-never-propagate", "".join(env.values()))
        finally:
            del os.environ["NVIDIA_API_KEY"]

    def test_home_is_the_supplied_dir(self):
        self.assertEqual(sandbox.minimal_env(home="/tmp/staging")["HOME"], "/tmp/staging")

    def test_pythonpath_only_when_given(self):
        self.assertNotIn("PYTHONPATH", sandbox.minimal_env(home="/tmp/s"))
        self.assertEqual(sandbox.minimal_env(home="/tmp/s", pythonpath="/tmp/s")["PYTHONPATH"],
                         "/tmp/s")

    def test_empty_values_dropped(self):
        """An empty LC_ALL must not become an empty-string override."""
        env = sandbox.minimal_env(home="/tmp/s")
        self.assertTrue(all(v for v in env.values()))


class TestExploitBlockedEndToEnd(unittest.TestCase):
    """The regression guard for the vulnerability this module was written for.

    Runs the exact proven attack through the real `run_script` path: recover
    the true home via pwd.getpwuid (routing around the HOME redirect), read
    the credential file by absolute path, and open a socket.

    Deliberately NOT a quiet `skipIf` on "no mechanism available". These are
    the only automated proof that the HIGH-severity finding stays fixed, and a
    silent skip is exactly how a security guard rots: CI would stay green on a
    host where nothing is enforced. Missing mechanism is therefore an ERROR,
    escapable only by opting in explicitly."""

    @classmethod
    def setUpClass(cls):
        if sandbox.mechanism() is not None:
            return
        if os.environ.get("COUNCIL_ALLOW_UNSANDBOXED_TESTS") == "1":
            raise unittest.SkipTest(
                "no sandbox mechanism; skip explicitly allowed via "
                "COUNCIL_ALLOW_UNSANDBOXED_TESTS=1")
        raise AssertionError(
            "No OS sandbox mechanism on this host, so the model-authored-script "
            "exploit guard cannot run. Install bubblewrap (Linux: "
            "`sudo apt-get install -y bubblewrap`); macOS ships sandbox-exec. "
            "To acknowledge running without that protection, set "
            "COUNCIL_ALLOW_UNSANDBOXED_TESTS=1.")

    def setUp(self):
        self.staging = Path(tempfile.mkdtemp(prefix="codecouncil-sbtest-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.staging, ignore_errors=True)

    def _run(self, src: str):
        # policy passed explicitly: these assertions must not depend on
        # whatever ~/.codecouncil/config.json happens to say on this host
        return run_script(self.staging, src, 60, policy=sandbox.POLICY_AUTO)

    def test_getpwuid_home_read_is_blocked(self):
        """HOME redirection alone did NOT stop this — getpwuid reads the OS
        user database, not the environment."""
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        probe = Path(real_home) / ".codecouncil"
        if not probe.exists():
            self.skipTest("no ~/.codecouncil on this host to attempt reading")
        res = self._run(
            "import os, pwd\n"
            "real = pwd.getpwuid(os.getuid()).pw_dir\n"
            "try:\n"
            "    open(os.path.join(real, '.codecouncil', 'env'), 'rb').read()\n"
            "    print('LEAKED')\n"
            "except Exception as e:\n"
            "    print('BLOCKED', type(e).__name__)\n"
        )
        self.assertNotIn("LEAKED", res.stdout)
        self.assertIn("BLOCKED", res.stdout)

    def test_network_egress_is_blocked(self):
        res = self._run(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=5).close()\n"
            "    print('NET-OPEN')\n"
            "except Exception as e:\n"
            "    print('NET-BLOCKED', type(e).__name__)\n"
        )
        self.assertNotIn("NET-OPEN", res.stdout)
        self.assertIn("NET-BLOCKED", res.stdout)

    def test_staging_still_works(self):
        """The sandbox must not break the feature it protects: verification
        stages a file, imports it, and writes scratch output."""
        (self.staging / "victim.py").write_text("VALUE = 41\n", encoding="utf-8")
        res = self._run(
            "import victim\n"
            "open('scratch.txt', 'w').write('ok')\n"
            "print('CONFIRMED:', victim.VALUE + 1)\n"
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CONFIRMED: 42", res.stdout)

    def test_policy_off_restores_unsandboxed_behavior(self):
        """The escape hatch must genuinely bypass — otherwise operators can't
        debug a profile that's over-denying."""
        res = run_script(self.staging, "print('RAN')", 60, policy=sandbox.POLICY_OFF)
        self.assertIn("RAN", res.stdout)


class TestScreenProbeIsolation(unittest.TestCase):
    """critic/screen.py runs a python probe with cwd=<untrusted repo>; -I must
    keep the repo off sys.path so a planted sitecustomize can never execute."""

    def test_isolated_mode_drops_cwd_from_syspath(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "sitecustomize.py").write_text(
                "import sys; print('PLANTED-CODE-RAN', file=sys.stderr)\n", encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-I", "-c", "import sys; print('' in sys.path)"],
                cwd=d, capture_output=True, text=True, timeout=30,
                env=sandbox.minimal_env(home=d))
            self.assertNotIn("PLANTED-CODE-RAN", r.stderr)
            self.assertIn("False", r.stdout)


if __name__ == "__main__":
    unittest.main()
