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
from critic.main import heartbeat, load_state


class TestParseReply(unittest.TestCase):
    def test_pass_variants(self):
        for raw in ("PASS", "pass", "Pass.", "  PASS\n", "```\nPASS\n```"):
            self.assertEqual(prompt.parse_reply(raw)["verdict"], "PASS", raw)

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


class TestHeartbeatWithStub(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
        self.obs = self.cc / "observations.ndjsonl"
        self.suggestions = self.cc / "suggestions.ndjsonl"
        self.heuristics = self.cc / "heuristics.md"
        self.stub = self.cc / "stub.sh"

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

    def test_no_new_events_makes_no_call(self):
        self.obs.write_text("")
        os.environ["CRITIC_CMD"] = "/nonexistent"  # would explode if called
        state = load_state(self.cc / "nope.json")
        heartbeat(self.obs, state, self.heuristics, self.suggestions, "sb", "ag")
        self.assertFalse(self.suggestions.exists())
        self.assertEqual(state["beat"], 1)

    def test_events_produce_logged_verdict(self):
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "reasoning", "session": "s",
             "payload": {"kind": "text", "text": "hello"}},
        ])
        state = load_state(self.cc / "nope.json")
        heartbeat(self.obs, state, self.heuristics, self.suggestions, "sb", "ag")
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "PASS")
        self.assertEqual(rows[0]["heuristics_version"], 1)  # seeded from heuristics.seed.md
        self.assertTrue(self.heuristics.exists())

    def test_suggestion_logged_and_offset_advances(self):
        self._set_stub('{"file": "x.py", "line": 2, "severity": "low", "issue": "i", "rationale": "r"}')
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+bad", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")
        heartbeat(self.obs, state, self.heuristics, self.suggestions, "sb", "ag")
        # second beat: nothing new -> no second row
        heartbeat(self.obs, state, self.heuristics, self.suggestions, "sb", "ag")
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "SUGGESTION")
        self.assertEqual(state["latest_diff"]["payload"]["diff"], "+bad")


if __name__ == "__main__":
    unittest.main()
