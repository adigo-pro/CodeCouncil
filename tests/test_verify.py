"""Verification tests: script-execution classification, prompt shape,
delivery policy for refuted.

Task: script-based verification. The model's reply IS a self-contained
Python script; the harness (not the model) executes it in the staging dir
and reads CONFIRMED:/REFUTED: off its captured stdout. This replaced an
earlier tool-enabled turn (read+bash) that asked the model to run its own
repro and report a status line -- the NVIDIA/pi backend frequently emitted
those tool calls as literal, never-executed text, landing verification
"inconclusive" and withholding true findings. Tests that exercised the old
raw-reply-is-a-status-line contract (verify.parse, verify.safe_repro, the
REPRO: line, VERIFY_TOOLS) are gone along with that code -- see the task
report for the full list.
"""

import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critic import verify
from hooks.logic import decide

NOW = time.time()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def _sugg(verification=None):
    row = {
        "id": "v1", "ts": _iso(NOW), "verdict": "SUGGESTION",
        "suggestion": {"file": "a.py", "line": 3, "severity": "high",
                       "issue": "boom", "rationale": "r"},
    }
    if verification:
        row["verification"] = verification
    return row


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_finding_and_path(self):
        text = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py")
        self.assertIn("TASK: VERIFY", text)
        self.assertIn("a.py:3", text)
        self.assertIn("/tmp/staging/a.py", text)

    def test_prompt_asks_for_confirmed_refuted_markers(self):
        text = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py")
        self.assertIn("CONFIRMED:", text)
        self.assertIn("REFUTED:", text)

    def test_prompt_asks_for_a_script_not_tool_calls(self):
        text = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py")
        self.assertIn("script", text.lower())
        self.assertIn("will be executed", text)


class TestVerifyFindingBasics(unittest.TestCase):
    def test_missing_file_is_inconclusive_without_any_call(self):
        v = verify.verify_finding(Path("/nonexistent-repo"), _sugg()["suggestion"])
        self.assertEqual(v["status"], "inconclusive")


class TestExploitAddenda(unittest.TestCase):
    """Task 4: a confirmed finding whose originating screen signal is a
    known exploitable CWE gets a prompt addendum instructing the verifier
    to DEMONSTRATE the vulnerability class in its script, not just re-read
    the code. No screen_signal (or an unknown/non-security cwe) must leave
    the prompt byte-identical to today."""

    def test_known_cwe_appends_addendum(self):
        base = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py")
        with_signal = verify.build_prompt(
            _sugg()["suggestion"], "/tmp/staging/a.py",
            screen_signal={"kind": "sql-injection", "cwe": "CWE-89"})
        self.assertNotEqual(base, with_signal)
        self.assertIn(verify.EXPLOIT_ADDENDA["CWE-89"], with_signal)
        self.assertIn("1 OR 1=1", with_signal)
        self.assertTrue(with_signal.startswith(base))

    def test_all_known_cwes_have_addenda_under_six_lines(self):
        for cwe in ("CWE-89", "CWE-78", "CWE-95", "CWE-502"):
            text = verify.EXPLOIT_ADDENDA[cwe]
            self.assertLessEqual(text.count("\n") + 1, 6, cwe)

    def test_screen_signal_none_is_byte_identical_to_today(self):
        a = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py")
        b = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py", screen_signal=None)
        self.assertEqual(a, b)

    def test_unknown_cwe_leaves_prompt_unchanged(self):
        a = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py")
        b = verify.build_prompt(_sugg()["suggestion"], "/tmp/staging/a.py",
                                screen_signal={"kind": "unresolvable-import",
                                               "cwe": "slopsquatting"})
        self.assertEqual(a, b)

    def test_verify_finding_threads_screen_signal_into_prompt_seen_by_model(self):
        """End-to-end: the stub's generated script prints a different
        CONFIRMED note depending on whether it saw the addendum marker text
        in its prompt file, proving verify_finding actually passes
        screen_signal through to the model call (not just build_prompt in
        isolation) -- the stub decides which script to hand back, the
        harness then executes whichever one it got."""
        td = tempfile.TemporaryDirectory()
        try:
            repo = Path(td.name)
            (repo / "app.py").write_text("cursor.execute(f'SELECT {x}')\n")
            stub = repo / "stub.sh"
            stub.write_text(
                "#!/bin/sh\n"
                "if grep -q 'DEMONSTRATE' \"$1\"; then "
                "printf 'print(\"CONFIRMED: saw addendum\")'; "
                "else printf 'print(\"CONFIRMED: no addendum\")'; fi\n"
            )
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            saved = os.environ.get("CRITIC_CMD")
            os.environ["CRITIC_CMD"] = str(stub)
            try:
                suggestion = {"file": "app.py", "line": 1, "severity": "high",
                             "issue": "SQL injection via f-string", "rationale": "r"}
                result = verify.verify_finding(
                    repo, suggestion,
                    screen_signal={"kind": "sql-injection", "cwe": "CWE-89"})
            finally:
                if saved is None:
                    os.environ.pop("CRITIC_CMD", None)
                else:
                    os.environ["CRITIC_CMD"] = saved
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["note"], "saw addendum")
        finally:
            td.cleanup()


class TestScriptVerification(unittest.TestCase):
    """The core of the port: verify_finding executes whatever script the
    model handed back and classifies the run, never trusting a status line
    the model merely asserted."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name)
        (self.repo / "a.py").write_text("def boom():\n    return 1 / 0\n")
        self._saved = os.environ.get("CRITIC_CMD")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CRITIC_CMD", None)
        else:
            os.environ["CRITIC_CMD"] = self._saved
        self.td.cleanup()

    def _stub_reply(self, reply: str) -> None:
        """A CRITIC_CMD stub whose stdout (the 'model reply') is exactly
        `reply`, regardless of the prompt it was given -- these tests are
        about how verify_finding executes and classifies a script, not
        about prompt content (that's TestBuildPrompt/TestExploitAddenda)."""
        stub = self.repo / "stub.py"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.stdout.write({reply!r})\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        os.environ["CRITIC_CMD"] = str(stub)

    def _suggestion(self):
        return {"file": "a.py", "line": 2, "severity": "high",
                "issue": "division by zero", "rationale": "r"}

    def test_confirmed_script_verifies_with_repro(self):
        self._stub_reply("print('CONFIRMED: raised ZeroDivisionError')\n")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["note"], "raised ZeroDivisionError")
        self.assertEqual(result["repro"], "print('CONFIRMED: raised ZeroDivisionError')")

    def test_refuted_script(self):
        self._stub_reply("print('REFUTED: no such bug, guarded on line 1')\n")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "refuted")
        self.assertEqual(result["note"], "no such bug, guarded on line 1")
        self.assertNotIn("repro", result)

    def test_raising_script_is_inconclusive_not_verified_or_refuted(self):
        self._stub_reply("raise RuntimeError('script bug, not a finding')\n")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")
        self.assertNotEqual(result["status"], "verified")
        self.assertNotEqual(result["status"], "refuted")
        self.assertIn("script bug, not a finding", result["note"])

    def test_timeout_script_is_inconclusive(self):
        self._stub_reply("import time\ntime.sleep(5)\n")
        with mock.patch.object(verify, "VERIFY_EXEC_TIMEOUT", 0.2):
            result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("timed out", result["note"])

    def test_fenced_reply_still_parses_and_executes(self):
        self._stub_reply("```python\nprint('CONFIRMED: fenced ok')\n```\n")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["note"], "fenced ok")
        self.assertNotIn("```", result["repro"])

    def test_both_markers_present_is_inconclusive(self):
        self._stub_reply(
            "print('CONFIRMED: looks bad')\nprint('REFUTED: actually fine')\n")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("both CONFIRMED and REFUTED", result["note"])

    def test_no_marker_is_inconclusive(self):
        self._stub_reply("print('just some output, no verdict')\n")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")

    def test_empty_reply_is_inconclusive_without_executing(self):
        self._stub_reply("")
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")

    def test_script_actually_runs_against_staged_file(self):
        # cwd=staging + PYTHONPATH=staging together mean the model's script
        # can import the staged module by name -- the same capability
        # critic/probe.py's probe scripts already rely on. Proves this is a
        # real execution, not a re-read of the model's claim.
        self._stub_reply(
            "import a\n"
            "try:\n"
            "    a.boom()\n"
            "    print('REFUTED: did not raise')\n"
            "except ZeroDivisionError:\n"
            "    print('CONFIRMED: a.boom() raised ZeroDivisionError')\n"
        )
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["note"], "a.boom() raised ZeroDivisionError")

    def test_agent_error_yields_error_status(self):
        stub = self.repo / "fail.sh"
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        os.environ["CRITIC_CMD"] = str(stub)
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "error")

    def test_marker_then_crash_is_inconclusive(self):
        """CONFIRMED marker printed but script exits nonzero → inconclusive,
        not verified. A script that crashes after printing the marker is not
        a clean verification."""
        self._stub_reply(
            "print('CONFIRMED: reproduced the issue')\n"
            "raise RuntimeError('unrelated crash after marker')\n"
        )
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("exit", result["note"].lower())
        self.assertNotIn("repro", result, "inconclusive should not have repro field")

    def test_refuted_marker_then_crash_is_inconclusive(self):
        """REFUTED marker printed but script exits nonzero → inconclusive,
        not refuted. A script that crashes after printing the marker is not
        a clean refutation."""
        self._stub_reply(
            "print('REFUTED: the code is correct')\n"
            "raise RuntimeError('unrelated crash after marker')\n"
        )
        result = verify.verify_finding(self.repo, self._suggestion())
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("exit", result["note"].lower())


class TestVerifyFindingReproIsExecutedScript(unittest.TestCase):
    """verify_finding's repro is now the executed script text itself
    (localized: any mention of the throwaway staging dir is rewritten to a
    repo-root-relative '.'), not a model-asserted REPRO: command line."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name)
        (self.repo / "a.py").write_text("1/0\n")
        self.stub = self.repo / "stub.sh"
        # The stub's script embeds the absolute staged path it was told
        # about in the prompt (grepped out of the prompt file) so the test
        # can confirm localize_repro rewrites it away.
        self.stub.write_text(
            "#!/bin/sh\n"
            "path=$(grep -o 'is at: .*' \"$1\" | sed 's/is at: //')\n"
            "printf 'with open(\"%s\"):\\n    pass\\nprint(\"CONFIRMED: reproduced\")\\n' \"$path\"\n"
        )
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        self._saved = os.environ.get("CRITIC_CMD")
        os.environ["CRITIC_CMD"] = str(self.stub)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CRITIC_CMD", None)
        else:
            os.environ["CRITIC_CMD"] = self._saved
        self.td.cleanup()

    def test_repro_is_script_text_with_staging_path_localized(self):
        suggestion = {"file": "a.py", "line": 1, "severity": "high",
                     "issue": "boom", "rationale": "r"}
        result = verify.verify_finding(self.repo, suggestion)
        self.assertEqual(result["status"], "verified")
        self.assertIn('open("./a.py")', result["repro"])
        self.assertNotIn(str(Path(self.td.name)), result["repro"])
        self.assertNotIn("codecouncil-verify-", result["repro"])


class TestLocalizeRepro(unittest.TestCase):
    def test_replaces_staging_prefix_with_dot(self):
        staging = Path("/tmp/codecouncil-verify-abc123")
        repro = verify.localize_repro(f"python3 {staging}/a.py", staging)
        self.assertEqual(repro, "python3 ./a.py")

    def test_no_staging_prefix_present_is_unchanged(self):
        staging = Path("/tmp/codecouncil-verify-abc123")
        repro = verify.localize_repro("python3 a.py", staging)
        self.assertEqual(repro, "python3 a.py")


class TestDeliveryPolicy(unittest.TestCase):
    def test_refuted_never_delivered(self):
        rows = [_sugg({"status": "refuted", "note": "guard exists"})]
        self.assertIsNone(decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW))
        self.assertIsNone(decide({"hook_event_name": "Stop", "cwd": "/x",
                                  "stop_hook_active": False}, rows, {}, NOW))

    def test_refuted_exploit_never_delivered(self):
        # a refuted finding whose repro was a real exploit attempt (Task 4)
        # follows the exact same never-delivered path as any other refuted
        # finding — the screen_signal field carries no separate delivery rule
        row = _sugg({"status": "refuted", "note": "input was safely parameterized"})
        row["screen_signal"] = {"kind": "sql-injection", "cwe": "CWE-89"}
        rows = [row]
        self.assertIsNone(decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW))
        self.assertIsNone(decide({"hook_event_name": "Stop", "cwd": "/x",
                                  "stop_hook_active": False}, rows, {}, NOW))

    def test_verified_delivers_with_proof(self):
        rows = [_sugg({"status": "verified", "note": "ZeroDivisionError raised"})]
        out = decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW)
        self.assertIn("verified by repro: ZeroDivisionError raised",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_inconclusive_and_unverified_still_deliver(self):
        for verification in (None, {"status": "inconclusive", "note": "n"},
                             {"status": "error", "note": "n"}):
            rows = [_sugg(verification)]
            out = decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW)
            self.assertIsNotNone(out, str(verification))

    def test_verified_with_repro_appends_verify_yourself_hint(self):
        rows = [_sugg({"status": "verified", "note": "ZeroDivisionError raised",
                       "repro": 'python3 -c "1/0"'})]
        out = decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn('[suggested repro (review before running): python3 -c "1/0"]', ctx)

    def test_verified_without_repro_no_verify_yourself_hint(self):
        rows = [_sugg({"status": "verified", "note": "ZeroDivisionError raised"})]
        out = decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("suggested repro", ctx)

    def test_verified_with_repro_appends_hint_on_block_channel_too(self):
        rows = [_sugg({"status": "verified", "note": "boom", "repro": "python3 repro.py"})]
        out = decide({"hook_event_name": "Stop", "cwd": "/x", "stop_hook_active": False},
                      rows, {}, NOW)
        self.assertIn("[suggested repro (review before running): python3 repro.py]", out["reason"])


if __name__ == "__main__":
    unittest.main()
