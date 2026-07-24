"""Critic tests: task review content and the claims-vs-verified session receipt."""

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critic import prompt


class TestTaskReview(unittest.TestCase):
    def test_tests_run_detection(self):
        def ev(cmd):
            return {"type": "tool_call", "payload": {"tool": "Bash", "input": {"command": cmd}}}

        self.assertEqual(prompt.tests_run([ev("python3 -m unittest discover")]), "python3 -m unittest")
        self.assertEqual(prompt.tests_run([ev("npm test -- --watch=false")]), "npm test")
        self.assertIsNone(prompt.tests_run([ev("git commit -m 'tests pass'")]))
        self.assertIsNone(prompt.tests_run([{"type": "reasoning", "payload": {"text": "ran pytest"}}]))

    def test_build_task_review_content(self):
        events = [
            {"type": "reasoning", "payload": {"kind": "text", "text": "All tests pass, edge cases handled."}},
            {"type": "commit", "payload": {"subjects": ["abc done"], "diff": "+code", "stat": ""}},
        ]
        text = prompt.build_task_review(events, None, "version: 2")
        self.assertIn("TASK REVIEW", text)
        self.assertIn("All tests pass, edge cases handled.", text)
        self.assertIn("NO test command was executed", text)
        self.assertIn("abc done", text)
        self.assertIn("UNSUPPORTED", text)

    def test_build_task_review_sticky_middle_state(self):
        """Task 9: no test command in the review window, but one ran earlier
        (possibly a different session — tests_run_sticky is a cross-session
        max, not scoped to the reviewed session) — the false 'no tests were
        run' flag this fixes."""
        events = [{"type": "reasoning", "payload": {"kind": "text", "text": "All done."}}]
        text = prompt.build_task_review(events, None, "version: 2",
                                        tests_run_sticky="2026-01-01T00:00:00+00:00")
        self.assertIn(
            "no test command in this window, but one ran at "
            "2026-01-01T00:00:00+00:00 earlier (possibly another session)", text)
        self.assertNotIn("NO test command was executed", text)

    def test_build_task_review_hard_no_tests_state_without_sticky(self):
        events = [{"type": "reasoning", "payload": {"kind": "text", "text": "All done."}}]
        text = prompt.build_task_review(events, None, "version: 2", tests_run_sticky=None)
        self.assertIn("NO test command was executed", text)

    def test_should_task_review_debounce(self):
        from critic.main import should_task_review
        now = 1_000_000.0
        state = {"material_since_review": True}
        self.assertTrue(should_task_review(state, 1, now))
        self.assertFalse(should_task_review(state, 0, now))  # no request
        self.assertFalse(should_task_review({"material_since_review": False}, 3, now))  # no material
        state["last_task_review"] = now - 10
        self.assertFalse(should_task_review(state, 1, now))  # cooldown
        state["last_task_review"] = now - 700
        self.assertTrue(should_task_review(state, 1, now))


class TestReceiptContent(unittest.TestCase):
    """Task 10: the human-facing receipt — claims extracted from the window,
    the mechanical test fact rendered verbatim, findings joined with outcomes,
    and bullet/file caps honored."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
        self.suggestions = self.cc / "suggestions.ndjsonl"

    def tearDown(self):
        self.td.cleanup()

    def test_claims_pulls_commit_subjects_and_claimy_reasoning(self):
        from critic.receipt import _claims

        events = [
            {"type": "commit", "payload": {"subjects": ["Fix parser crash"]}},
            {"type": "reasoning", "payload": {"text": "Let me look at the file next."}},
            {"type": "reasoning", "payload": {"text": "I implemented the missing handler."}},
        ]
        claims = _claims(events)
        self.assertIn("Fix parser crash", claims)
        self.assertIn("I implemented the missing handler.", claims)
        self.assertNotIn("Let me look at the file next.", claims)  # no claim verb

    def test_claims_capped_at_six_and_truncated(self):
        from critic.receipt import _claims, MAX_CLAIM_BULLETS, CLAIM_TRUNCATE_CHARS

        events = [
            {"type": "commit", "payload": {"subjects": [f"fix issue {i}" for i in range(10)]}},
        ]
        claims = _claims(events)
        self.assertEqual(len(claims), MAX_CLAIM_BULLETS)

        long_text = "I fixed the bug and " + "x" * 300
        events = [{"type": "reasoning", "payload": {"text": long_text}}]
        claims = _claims(events)
        self.assertEqual(len(claims), 1)
        self.assertLessEqual(len(claims[0]), CLAIM_TRUNCATE_CHARS)
        self.assertTrue(claims[0].endswith("…"))

    def test_files_changed_from_latest_diff_stat(self):
        from critic.receipt import _files_changed

        events = [
            {"type": "diff", "payload": {"stat": " a.py | 2 +-\n b.py | 1 +\n"
                                                 " 2 files changed, 3 insertions(+)\n"}},
        ]
        self.assertEqual(_files_changed(events), 2)
        self.assertIsNone(_files_changed([{"type": "reasoning", "payload": {"text": "x"}}]))

    def test_findings_joined_with_outcomes_and_windowed(self):
        from critic.receipt import _findings

        with self.suggestions.open("a") as f:
            f.write(json.dumps({
                "id": "s1", "ts": "2026-01-01T00:05:00+00:00", "verdict": "SUGGESTION",
                "suggestion": {"file": "a.py", "issue": "leak", "severity": "high"},
                "verification": {"status": "verified", "note": "repro"},
            }) + "\n")
            f.write(json.dumps({
                "id": "s2", "ts": "2026-01-01T00:06:00+00:00", "verdict": "SUGGESTION",
                "suggestion": {"file": "b.py", "issue": "typo", "severity": "low"},
            }) + "\n")
            f.write(json.dumps({  # outside the window: must not appear
                "id": "s3", "ts": "2026-01-01T00:00:00+00:00", "verdict": "SUGGESTION",
                "suggestion": {"file": "c.py", "issue": "old", "severity": "low"},
            }) + "\n")
        outcomes = self.cc / "outcomes.ndjsonl"
        outcomes.write_text(json.dumps({"suggestion_id": "s1", "outcome": "accepted"}) + "\n")

        since = datetime.fromisoformat("2026-01-01T00:01:00+00:00").timestamp()
        now = datetime.fromisoformat("2026-01-01T00:10:00+00:00").timestamp()
        findings = _findings(self.suggestions, since, now)
        by_id = {f["file"]: f for f in findings}
        self.assertEqual(set(by_id), {"a.py", "b.py"})
        self.assertEqual(by_id["a.py"]["outcome"], "accepted")
        self.assertEqual(by_id["a.py"]["verification"], "verified")
        self.assertEqual(by_id["b.py"]["outcome"], "pending")

    def test_write_receipt_renders_all_sections(self):
        from critic.receipt import write_receipt

        events = [
            {"type": "commit", "session": "sess-1", "payload": {"subjects": ["Fix parser crash"]}},
            {"type": "diff", "session": "sess-1",
             "payload": {"stat": " a.py | 2 +-\n 1 file changed, 2 insertions(+)\n"}},
        ]
        record = {"verdict": "PASS", "heuristics_version": 3}
        tests_fact = "MECHANICAL FACT — tests run in this window (pytest)"
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}

        path = write_receipt(self.cc, ctx_like, events, record, tests_fact)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.cc / "receipts")
        text = path.read_text()
        self.assertIn("# CodeCouncil Session Receipt", text)
        self.assertIn("Verdict: PASS", text)
        self.assertIn("Fix parser crash", text)
        self.assertIn(tests_fact, text)
        self.assertIn("files changed (latest diff): 1", text)
        self.assertIn("## Findings this session", text)
        self.assertIn("(none)", text)
        self.assertIn("heuristics v3", text)

    def test_write_receipt_suggestion_verdict_shows_issue(self):
        from critic.receipt import write_receipt

        record = {
            "verdict": "SUGGESTION", "heuristics_version": 1,
            "suggestion": {"file": "x.py", "issue": "unsupported claim"},
        }
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}
        path = write_receipt(self.cc, ctx_like, [], record, "MECHANICAL FACT — NO test command was executed")
        text = path.read_text()
        self.assertIn("ISSUE — x.py: unsupported claim", text)

    def test_write_receipt_renders_test_integrity_weakened(self):
        from critic.receipt import parse_test_integrity, write_receipt

        record = {"verdict": "PASS", "heuristics_version": 1}
        ti = {"verdict": "weakened", "tests_added": 0, "tests_removed": 0,
              "asserts_added": 1, "asserts_removed": 3}
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}

        path = write_receipt(self.cc, ctx_like, [], record, "fact", test_integrity=ti)
        text = path.read_text()
        self.assertIn("## Test integrity", text)
        self.assertIn("tests: weakened — 3 assertion(s) removed, 1 added", text)
        self.assertEqual(parse_test_integrity(text), ti)

    def test_write_receipt_default_test_integrity_is_unchanged(self):
        from critic.receipt import parse_test_integrity, write_receipt

        record = {"verdict": "PASS", "heuristics_version": 1}
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}

        path = write_receipt(self.cc, ctx_like, [], record, "fact")
        text = path.read_text()
        self.assertIn("tests: unchanged", text)
        self.assertEqual(parse_test_integrity(text), {
            "verdict": "unchanged", "tests_added": 0, "tests_removed": 0,
            "asserts_added": 0, "asserts_removed": 0,
        })

    def test_write_receipt_renders_test_integrity_strengthened(self):
        from critic.receipt import parse_test_integrity, write_receipt

        record = {"verdict": "PASS", "heuristics_version": 1}
        ti = {"verdict": "strengthened", "tests_added": 1, "tests_removed": 0,
              "asserts_added": 2, "asserts_removed": 0}
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}

        path = write_receipt(self.cc, ctx_like, [], record, "fact", test_integrity=ti)
        text = path.read_text()
        self.assertIn("tests: strengthened — 1 test(s) added, 2 assertion(s) added", text)
        self.assertEqual(parse_test_integrity(text), ti)

    def test_write_receipt_renders_dependencies_section_when_present(self):
        """Task 3: new_dependency_lines results get their own section."""
        from critic.receipt import write_receipt

        record = {"verdict": "PASS", "heuristics_version": 1}
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}

        path = write_receipt(self.cc, ctx_like, [], record, "fact",
                             new_dependencies=["requests==2.31.0", "flask>=2.0"])
        text = path.read_text()
        self.assertIn("## Dependencies added this session", text)
        self.assertIn("- requests==2.31.0", text)
        self.assertIn("- flask>=2.0", text)

    def test_write_receipt_omits_dependencies_section_when_empty(self):
        """Empty/None new_dependencies -> section absent, old rendering unchanged."""
        from critic.receipt import write_receipt

        record = {"verdict": "PASS", "heuristics_version": 1}
        ctx_like = {"repo": self.cc, "suggestions_file": self.suggestions, "since_epoch": 0.0}

        path_none = write_receipt(self.cc, ctx_like, [], record, "fact")
        path_empty = write_receipt(self.cc, ctx_like, [], record, "fact", new_dependencies=[])
        self.assertNotIn("## Dependencies added this session", path_none.read_text())
        self.assertNotIn("## Dependencies added this session", path_empty.read_text())

    def test_parse_test_integrity_missing_block_returns_none(self):
        from critic.receipt import parse_test_integrity

        self.assertIsNone(parse_test_integrity("# just a receipt\nno fence here\n"))

    def test_prune_to_fifty(self):
        from critic.receipt import write_receipt, RECEIPTS_KEEP

        record = {"verdict": "PASS", "heuristics_version": 1}
        for i in range(RECEIPTS_KEEP + 5):
            events = [{"type": "reasoning", "session": f"sess-{i}", "payload": {"text": "done"}}]
            write_receipt(self.cc, {"repo": self.cc, "suggestions_file": self.suggestions,
                                    "since_epoch": 0.0}, events, record, "fact")
        remaining = list((self.cc / "receipts").glob("*.md"))
        self.assertEqual(len(remaining), RECEIPTS_KEEP)


class TestReceiptTaskReviewIntegration(unittest.TestCase):
    """Task 10: task_review writes a receipt after appending its record, and a
    receipt failure must never break the review itself."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
        self.obs = self.cc / "observations.ndjsonl"
        self.suggestions = self.cc / "suggestions.ndjsonl"
        self.heuristics = self.cc / "heuristics.md"
        self.stub = self.cc / "stub.sh"
        self.ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
                    "persona": "", "project": "", "repo": self.cc, "verify": False}

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def _set_stub(self, reply: str):
        self.stub.write_text(f"#!/bin/sh\necho '{reply}'\n")
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        os.environ["CRITIC_CMD"] = str(self.stub)

    def _write_obs(self, events):
        with self.obs.open("a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_task_review_writes_a_receipt(self):
        from critic.main import task_review

        self._set_stub("PASS")
        self._write_obs([{
            "ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "commit",
            "payload": {"subjects": ["Fix the bug"], "diff": "", "stat": ""},
        }])
        since = datetime.fromisoformat("2025-12-31T00:00:00+00:00").timestamp()
        task_review(self.obs, {**self.ctx, "beat": 1, "ts": "2026-01-01T00:00:01+00:00"},
                   since_epoch=since)
        receipts = list((self.cc / "receipts").glob("*.md"))
        self.assertEqual(len(receipts), 1)
        self.assertIn("Fix the bug", receipts[0].read_text())

    def test_task_review_receipt_reflects_weakened_test_diff(self):
        """Task 2: task_review must pass the session's batch diffs (commit +
        latest working-tree diff, same material judge_batch screens) into
        receipt writing so the receipt's test_integrity block reflects what
        actually happened, not the inert default."""
        from critic.main import task_review
        from critic.receipt import parse_test_integrity

        self._set_stub("PASS")
        weakening_diff = (
            "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,5 +1,5 @@\n"
            "-    assert x == 1\n"
            "-    assert y == 2\n"
            "+    pass\n"
        )
        self._write_obs([{
            "ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "commit",
            "payload": {"subjects": ["Simplify test_foo"], "diff": weakening_diff, "stat": ""},
        }])
        since = datetime.fromisoformat("2025-12-31T00:00:00+00:00").timestamp()
        task_review(self.obs, {**self.ctx, "beat": 1, "ts": "2026-01-01T00:00:01+00:00"},
                   since_epoch=since)
        receipts = list((self.cc / "receipts").glob("*.md"))
        self.assertEqual(len(receipts), 1)
        text = receipts[0].read_text()
        self.assertIn("tests: weakened", text)
        ti = parse_test_integrity(text)
        self.assertEqual(ti["verdict"], "weakened")
        self.assertEqual(ti["asserts_removed"], 2)
        self.assertEqual(ti["asserts_added"], 0)

    def test_task_review_receipt_reflects_new_dependency_lines(self):
        """Task 3: task_review must pass the session's batch diffs into
        deps.new_dependency_lines so the receipt's dependencies section
        reflects what this session's diffs actually added."""
        from critic.main import task_review

        self._set_stub("PASS")
        dep_diff = (
            "diff --git a/requirements.txt b/requirements.txt\n"
            "--- a/requirements.txt\n"
            "+++ b/requirements.txt\n"
            "@@ -1,1 +1,2 @@\n"
            " flask==2.0.0\n"
            "+requests==2.31.0\n"
        )
        self._write_obs([{
            "ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "commit",
            "payload": {"subjects": ["Add requests dependency"], "diff": dep_diff, "stat": ""},
        }])
        since = datetime.fromisoformat("2025-12-31T00:00:00+00:00").timestamp()
        task_review(self.obs, {**self.ctx, "beat": 1, "ts": "2026-01-01T00:00:01+00:00"},
                   since_epoch=since)
        receipts = list((self.cc / "receipts").glob("*.md"))
        self.assertEqual(len(receipts), 1)
        text = receipts[0].read_text()
        self.assertIn("## Dependencies added this session", text)
        self.assertIn("- requests==2.31.0", text)

    def test_task_review_survives_unwritable_receipts_dir(self):
        from critic.main import task_review

        self._set_stub("PASS")
        self._write_obs([{
            "ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "reasoning",
            "payload": {"kind": "text", "text": "done"},
        }])
        since = datetime.fromisoformat("2025-12-31T00:00:00+00:00").timestamp()
        with mock.patch("critic.main.receipt.write_receipt", side_effect=OSError("boom")):
            task_review(self.obs, {**self.ctx, "beat": 1, "ts": "2026-01-01T00:00:01+00:00"},
                       since_epoch=since)
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "PASS")
        self.assertFalse((self.cc / "receipts").exists())


if __name__ == "__main__":
    unittest.main()
