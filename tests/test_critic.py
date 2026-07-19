"""Critic tests: reply parsing, prompt building, and the loop with a stubbed agent."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critic import prompt
from critic.main import TurnScheduler, heartbeat, load_state, verdict_history


class TestParseReply(unittest.TestCase):
    def test_pass_variants(self):
        for raw in ("PASS", "pass", "Pass.", "  PASS\n", "```\nPASS\n```"):
            self.assertEqual(prompt.parse_reply(raw)["verdict"], "PASS", raw)

    def test_pass_with_reason(self):
        v = prompt.parse_reply("PASS: mid-edit, judging next beat")
        self.assertEqual(v["verdict"], "PASS")
        self.assertEqual(v["reason"], "mid-edit, judging next beat")
        v = prompt.parse_reply("pass — docs-only change")
        self.assertEqual(v["reason"], "docs-only change")
        self.assertNotIn("reason", prompt.parse_reply("PASS"))

    def test_long_prose_starting_with_pass_is_malformed(self):
        v = prompt.parse_reply("Passing along my thoughts: " + "x" * 400)
        self.assertEqual(v["verdict"], "PASS")
        self.assertIn("malformed", v)

    def test_valid_suggestion(self):
        raw = '{"file": "a.py", "line": 3, "severity": "high", "issue": "boom", "rationale": "r"}'
        v = prompt.parse_reply(raw)
        self.assertEqual(v["verdict"], "SUGGESTION")
        self.assertEqual(v["suggestion"]["file"], "a.py")
        self.assertEqual(v["suggestion"]["line"], 3)

    def test_suggestion_in_fences_and_prose(self):
        raw = 'Sure! Here you go:\n```json\n{"file": "b.py", "issue": "bad"}\n```'
        v = prompt.parse_reply(raw)
        self.assertEqual(v["verdict"], "SUGGESTION")
        self.assertEqual(v["suggestion"]["severity"], "medium")

    def test_malformed_treated_as_pass(self):
        v = prompt.parse_reply("I think this code looks great overall, but...")
        self.assertEqual(v["verdict"], "PASS")
        self.assertIn("malformed", v)

    def test_json_missing_required_keys_is_pass(self):
        v = prompt.parse_reply('{"severity": "high"}')
        self.assertEqual(v["verdict"], "PASS")


class TestPrompt(unittest.TestCase):
    def _events(self):
        return [
            {"type": "reasoning", "payload": {"kind": "thinking", "text": "I will cache users"}},
            {"type": "tool_call", "payload": {"tool": "Edit", "input": {"file_path": "a.py"}}},
        ]

    def test_sections_present(self):
        diff = {"type": "diff", "payload": {"diff": "+x = 1", "untracked": ["new.py"]}}
        text = prompt.build_prompt(self._events(), diff, "version: 3\n- rule")
        self.assertIn("HEURISTICS (v3):", text)
        self.assertIn("I will cache users", text)
        self.assertIn("Edit a.py", text)
        self.assertIn("+x = 1", text)
        self.assertIn("new.py", text)

    def test_no_diff(self):
        text = prompt.build_prompt(self._events(), None, "version: 1")
        self.assertIn("(no uncommitted changes)", text)

    def test_new_file_contents_rendered(self):
        diff = {"type": "diff", "payload": {"diff": "", "untracked": ["new.py"],
                                            "untracked_contents": {"new.py": "def g(): pass"}}}
        text = prompt.build_prompt(self._events(), diff, "version: 1")
        self.assertIn("NEW FILES (not yet committed):", text)
        self.assertIn("def g(): pass", text)

    def test_project_header_first(self):
        text = prompt.build_prompt(self._events(), None, "version: 1",
                                   project="PROJECT: demo (/x)\nREADME: a demo")
        self.assertTrue(text.startswith("PROJECT: demo"))
        self.assertIn("README: a demo", text)

    def test_version_default_zero(self):
        self.assertEqual(prompt.heuristics_version("no header"), 0)

    def test_verdict_history_rendered_with_instruction(self):
        history = [{"outcome": "rebutted", "file": "s.json", "line": None, "issue": "wrong dir"},
                   {"outcome": "pending", "file": "a.py", "line": 2, "issue": "leak"}]
        text = prompt.build_prompt(self._events(), None, "version: 1", verdict_history=history)
        self.assertIn("[rebutted] s.json — wrong dir", text)
        self.assertIn("[pending] a.py:2 — leak", text)
        self.assertIn("a rebutted finding is settled", text)

    def test_no_history_no_section(self):
        text = prompt.build_prompt(self._events(), None, "version: 1")
        self.assertNotIn("RECENT VERDICTS", text)

    def test_commit_events_rendered_and_open_gate(self):
        events = self._events() + [{"type": "commit", "payload": {
            "subjects": ["abc123 fix the frobnicator"], "diff": "+frob()", "stat": ""}}]
        text = prompt.build_prompt(events, None, "version: 1")
        self.assertIn("JUST COMMITTED:", text)
        self.assertIn("fix the frobnicator", text)
        self.assertIn("+frob()", text)
        s = TurnScheduler(judge_fn=lambda b, c: None)
        self.assertEqual(s.submit([{"type": "commit"}], {}), "dispatched")

    def test_windowing_caps_and_omission_note(self):
        events = ([{"type": "reasoning", "payload": {"kind": "text", "text": f"r{i}"}} for i in range(30)]
                  + [{"type": "tool_call", "payload": {"tool": "Edit", "input": {"file_path": f"f{i}.py"}}}
                     for i in range(70)])
        text = prompt.build_prompt(events, None, "version: 1")
        self.assertEqual(text.count("- Edit f"), 15)
        self.assertIn("(+77 earlier events this batch omitted)", text)  # 22 reasoning + 55 tools

    def test_prompt_budget_trims_diff_last(self):
        events = [{"type": "reasoning", "payload": {"kind": "text", "text": "x" * 1500}}
                  for _ in range(8)]
        diff = {"type": "diff", "payload": {"diff": "d" * 30_000, "untracked": []}}
        text = prompt.build_prompt(events, diff, "version: 1")
        self.assertLess(len(text), prompt.PROMPT_BUDGET_CHARS + prompt.MIN_DIFF_CHARS + 2_000)
        self.assertIn("… [truncated]", text)
        self.assertIn("x" * 100, text)  # reasoning survived; diff took the cut


class TestHeartbeatWithStub(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
        self.obs = self.cc / "observations.ndjsonl"
        self.suggestions = self.cc / "suggestions.ndjsonl"
        self.heuristics = self.cc / "heuristics.md"
        self.stub = self.cc / "stub.sh"
        self.ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
                    "sandbox": "sb", "agent": "ag", "project": "",
                    "repo": self.cc, "verify": False}

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

    def _beat(self, state, scheduler):
        status = heartbeat(self.obs, state, scheduler, self.ctx)
        if scheduler.thread:
            scheduler.thread.join()
        return status

    def test_no_new_events_makes_no_call(self):
        self.obs.write_text("")
        os.environ["CRITIC_CMD"] = "/nonexistent"  # would explode if called
        state = load_state(self.cc / "nope.json")
        status = self._beat(state, TurnScheduler())
        self.assertEqual(status, "idle")
        self.assertFalse(self.suggestions.exists())
        self.assertEqual(state["beat"], 1)

    def test_reasoning_only_is_gated_no_call(self):
        os.environ["CRITIC_CMD"] = "/nonexistent"  # would explode if called
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "reasoning", "session": "s",
             "payload": {"kind": "text", "text": "thinking out loud"}},
        ])
        state = load_state(self.cc / "nope.json")
        scheduler = TurnScheduler()
        self.assertEqual(self._beat(state, scheduler), "gated")
        self.assertFalse(self.suggestions.exists())
        self.assertEqual(len(scheduler.pending), 1)  # held, not dropped

    def test_gated_events_merge_into_next_diff_batch(self):
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "reasoning", "session": "s",
             "payload": {"kind": "text", "text": "planning"}},
        ])
        state = load_state(self.cc / "nope.json")
        scheduler = TurnScheduler()
        self._beat(state, scheduler)  # gated
        self._write_obs([
            {"ts": "t", "beat": 2, "type": "diff", "session": None,
             "payload": {"diff": "+code", "stat": "", "untracked": []}},
        ])
        self.assertEqual(self._beat(state, scheduler), "dispatched")
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n_events"], 2)  # merged: held reasoning + new diff

    def test_judge_every_beat_bypasses_gate(self):
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "reasoning", "session": "s",
             "payload": {"kind": "text", "text": "hello"}},
        ])
        state = load_state(self.cc / "nope.json")
        status = self._beat(state, TurnScheduler(judge_every_beat=True))
        self.assertEqual(status, "dispatched")
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        self.assertEqual(rows[0]["verdict"], "PASS")
        self.assertEqual(rows[0]["heuristics_version"], 1)  # seeded from heuristics.seed.md
        # audit trail: the exact prompt is saved under the verdict id
        saved = (self.cc / "prompts" / f"{rows[0]['id']}.txt").read_text()
        self.assertIn("hello", saved)
        self.assertEqual(rows[0]["prompt_chars"], len(saved))

    def test_suggestion_logged_and_offset_advances(self):
        self._set_stub('{"file": "x.py", "line": 2, "severity": "low", "issue": "i", "rationale": "r"}')
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+bad", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")
        scheduler = TurnScheduler()
        self._beat(state, scheduler)
        # second beat: nothing new -> no second row
        self._beat(state, scheduler)
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "SUGGESTION")
        self.assertEqual(state["latest_diff"]["payload"]["diff"], "+bad")

    def test_verdict_history_joins_outcomes(self):
        self.suggestions.write_text(json.dumps({
            "id": "s1", "verdict": "SUGGESTION",
            "suggestion": {"file": "a.py", "line": 1, "severity": "high", "issue": "i1"}}) + "\n")
        outcomes = self.cc / "outcomes.ndjsonl"
        outcomes.write_text(json.dumps({"suggestion_id": "s1", "outcome": "rebutted"}) + "\n")
        h = verdict_history(self.suggestions, outcomes)
        self.assertEqual(h, [{"outcome": "rebutted", "file": "a.py", "line": 1, "issue": "i1"}])
        self.assertEqual(verdict_history(self.suggestions, self.cc / "missing.ndjsonl")[0]["outcome"],
                         "pending")


class TestTaskReview(unittest.TestCase):
    def test_tests_run_detection(self):
        ev = lambda cmd: {"type": "tool_call", "payload": {"tool": "Bash", "input": {"command": cmd}}}
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


class TestSchedulerCooldown(unittest.TestCase):
    def test_spacing_holds_then_drain_bypasses(self):
        calls = []
        s = TurnScheduler(judge_fn=lambda b, c: calls.append(list(b)),
                          judge_every_beat=True, min_spacing=9999)
        self.assertEqual(s.submit([{"type": "diff"}], {}), "dispatched")
        s.thread.join()
        self.assertEqual(s.submit([{"type": "diff"}], {}), "cooling")  # within spacing
        self.assertEqual(len(s.pending), 1)  # held, not dropped
        s.drain({})
        self.assertEqual([len(c) for c in calls], [1, 1])  # drain flushed the held batch

    def test_zero_spacing_dispatches_back_to_back(self):
        calls = []
        s = TurnScheduler(judge_fn=lambda b, c: calls.append(1), judge_every_beat=True)
        s.submit([{"type": "diff"}], {})
        s.thread.join()
        self.assertEqual(s.submit([{"type": "diff"}], {}), "dispatched")
        s.thread.join()
        self.assertEqual(len(calls), 2)


class TestSchedulerAsync(unittest.TestCase):
    def test_busy_queues_then_merges(self):
        import threading as th
        release, calls = th.Event(), []

        def slow_judge(batch, ctx):
            release.wait(timeout=5)
            calls.append(list(batch))

        s = TurnScheduler(judge_fn=slow_judge, judge_every_beat=True)
        self.assertEqual(s.submit([{"type": "diff"}], {}), "dispatched")
        self.assertEqual(s.submit([{"type": "reasoning"}], {}), "busy")  # returns immediately
        self.assertEqual(s.submit([{"type": "tool_call"}], {}), "busy")
        release.set()
        s.thread.join()
        self.assertEqual(s.submit([], {}), "dispatched")  # flush the queued pair
        s.thread.join()
        self.assertEqual([len(c) for c in calls], [1, 2])


if __name__ == "__main__":
    unittest.main()
