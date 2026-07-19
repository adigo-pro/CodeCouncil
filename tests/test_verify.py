"""Verification tests: reply parsing, prompt shape, delivery policy for refuted."""

import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

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


class TestParse(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(verify.parse("VERIFIED: ZeroDivisionError raised"),
                         {"status": "verified", "note": "ZeroDivisionError raised"})
        self.assertEqual(verify.parse("REFUTED: guard exists on line 2")["status"], "refuted")
        self.assertEqual(verify.parse("INCONCLUSIVE: needs DB")["status"], "inconclusive")

    def test_takes_last_matching_line(self):
        raw = "Running repro...\nsome tool output\nVERIFIED: got the exception"
        self.assertEqual(verify.parse(raw)["note"], "got the exception")

    def test_garbage_is_inconclusive(self):
        v = verify.parse("I think it is probably fine")
        self.assertEqual(v["status"], "inconclusive")
        self.assertIn("unparseable", v["note"])

    def test_prompt_contains_finding_and_path(self):
        text = verify.build_prompt(_sugg()["suggestion"], "/sandbox/x/a.py")
        self.assertIn("TASK: VERIFY", text)
        self.assertIn("a.py:3", text)
        self.assertIn("/sandbox/x/a.py", text)

    def test_missing_file_is_inconclusive_without_any_call(self):
        v = verify.verify_finding(Path("/nonexistent-repo"), _sugg()["suggestion"], "sb", "ag")
        self.assertEqual(v["status"], "inconclusive")


class TestDeliveryPolicy(unittest.TestCase):
    def test_refuted_never_delivered(self):
        rows = [_sugg({"status": "refuted", "note": "guard exists"})]
        self.assertIsNone(decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW))
        self.assertIsNone(decide({"hook_event_name": "Stop", "cwd": "/x",
                                  "stop_hook_active": False}, rows, {}, NOW))

    def test_verified_delivers_with_proof(self):
        rows = [_sugg({"status": "verified", "note": "ZeroDivisionError raised"})]
        out = decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW)
        self.assertIn("verified in sandbox: ZeroDivisionError raised",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_inconclusive_and_unverified_still_deliver(self):
        for verification in (None, {"status": "inconclusive", "note": "n"},
                             {"status": "error", "note": "n"}):
            rows = [_sugg(verification)]
            out = decide({"hook_event_name": "PostToolUse", "cwd": "/x"}, rows, {}, NOW)
            self.assertIsNotNone(out, str(verification))


if __name__ == "__main__":
    unittest.main()
