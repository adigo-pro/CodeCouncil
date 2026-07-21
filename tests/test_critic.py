"""Critic tests: reply parsing, prompt building, and the loop with a stubbed agent."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critic import prompt
from critic.main import TurnScheduler, heartbeat, load_state, verdict_history
from critic.render import render_verdict


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

    def test_redaction_marker_survives_into_prompt(self):
        # Redaction happens upstream at observer capture time (observer/gitwatch.py);
        # build_prompt must not mangle the marker on its way into the model's context.
        diff = {"type": "diff", "payload": {
            "diff": "+aws_key = '«REDACTED:aws-key»'", "untracked": [],
        }}
        text = prompt.build_prompt(self._events(), diff, "version: 1")
        self.assertIn("«REDACTED:aws-key»", text)

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
                    "persona": "", "project": "",
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

    def test_record_has_dispatched_ts_and_ts_at_write_time(self):
        """Task 3: record["ts"] is stamped at write time, not dispatch time."""
        from datetime import datetime
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+code", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Both fields present
        self.assertIn("dispatched_ts", row)
        self.assertIn("ts", row)
        # ts >= dispatched_ts (model call + verification adds latency)
        dispatched = datetime.fromisoformat(row["dispatched_ts"]).timestamp()
        written = datetime.fromisoformat(row["ts"]).timestamp()
        self.assertGreaterEqual(written, dispatched)

    def test_ts_is_usable_by_age_ok(self):
        """Task 3: record["ts"] at write time is usable by hooks/logic._age_ok."""
        from datetime import datetime
        from hooks import logic
        import time
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+code", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        row = rows[0]
        # The written ts should pass the age check (TTL is 600s)
        now = time.time()
        self.assertTrue(logic._age_ok(row, now),
                        f"Age check failed for ts={row['ts']}, now={now}")

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

    def test_heartbeat_records_sticky_tests_run_at_for_session(self):
        """Task 9: a test command is credited to its session in state even
        when the beat carries no diff/commit (so it never dispatches to the
        scheduler) — the sticky fact must survive independent of the gate."""
        from datetime import datetime, timezone
        fresh_ts = datetime.now(timezone.utc).isoformat()
        self._write_obs([
            {"ts": fresh_ts, "beat": 1, "type": "tool_call", "session": "sess-A",
             "payload": {"tool": "Bash", "input": {"command": "python3 -m unittest discover"}}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        self.assertEqual(state["tests_run_at"].get("sess-A"), fresh_ts)

    def test_heartbeat_ignores_non_test_commands(self):
        self._write_obs([
            {"ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "tool_call",
             "session": "sess-A",
             "payload": {"tool": "Bash", "input": {"command": "git status"}}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        self.assertNotIn("tests_run_at", state)

    def test_heartbeat_skips_sessionless_test_command(self):
        """A test command with no session (session: None) must not be
        credited — there is no session to credit it to, and it would
        otherwise land as a JSON "null" key on persist."""
        from datetime import datetime, timezone
        self._write_obs([
            {"ts": datetime.now(timezone.utc).isoformat(), "beat": 1, "type": "tool_call",
             "session": None,
             "payload": {"tool": "Bash", "input": {"command": "python3 -m unittest discover"}}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        self.assertNotIn("tests_run_at", state)

    def test_heartbeat_prunes_stale_tests_run_at_entries(self):
        """tests_run_at entries older than the 24h staleness window are
        dropped at record time so the dict can't grow unbounded."""
        from datetime import datetime, timedelta, timezone
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = load_state(self.cc / "nope.json")
        state["tests_run_at"] = {"sess-old": stale_ts}
        self._write_obs([
            {"ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "tool_call",
             "session": "sess-new",
             "payload": {"tool": "Bash", "input": {"command": "git status"}}},
        ])
        self._beat(state, TurnScheduler())
        self.assertNotIn("sess-old", state["tests_run_at"])

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

    def test_verdict_history_reads_via_bounded_tail(self):
        """suggestions.ndjsonl / outcomes.ndjsonl grow unbounded over a
        session; verdict_history only needs the last few rows, so it must
        read both via read_tail_rows rather than a whole-file read."""
        import critic.main as main_mod
        calls = []

        def fake_tail(path, *a, **k):
            calls.append(path)
            return []

        with mock.patch.object(main_mod, "read_tail_rows", fake_tail):
            main_mod.verdict_history(self.suggestions, self.cc / "outcomes.ndjsonl")
        self.assertIn(self.suggestions, calls)
        self.assertIn(self.cc / "outcomes.ndjsonl", calls)


class TestNormalizeFile(unittest.TestCase):
    def test_staging_and_absolute_paths_map_back(self):
        from critic.main import normalize_file
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "config.py").write_text("x = 1")
            self.assertEqual(normalize_file(repo, "underreview/d4ab55df-config.py"), "config.py")
            self.assertEqual(
                normalize_file(repo, "/sandbox/workspaces/critic/underreview/ab12cd34-config.py"),
                "config.py")
            self.assertEqual(normalize_file(repo, str(repo / "config.py")), "config.py")
            self.assertEqual(normalize_file(repo, "config.py"), "config.py")
            self.assertEqual(normalize_file(repo, "nowhere.py"), "nowhere.py")  # unresolvable: keep


class TestAskWithRetry(unittest.TestCase):
    def test_malformed_then_clean_recovers(self):
        from critic.main import ask_with_retry
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "called-once"
            stub = Path(td) / "stub.sh"
            stub.write_text(
                f"#!/bin/sh\nif [ -f {marker} ]; then echo 'PASS'; else touch {marker}; "
                "echo 'garbled nonsense reply'; fi\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            os.environ["CRITIC_CMD"] = str(stub)
            try:
                v = ask_with_retry("prompt", {"persona": ""})
            finally:
                os.environ.pop("CRITIC_CMD", None)
            self.assertEqual(v["verdict"], "PASS")
            self.assertNotIn("malformed", v)

    def test_double_failure_keeps_last(self):
        from critic.main import ask_with_retry
        os.environ["CRITIC_CMD"] = "/nonexistent-cmd"
        try:
            v = ask_with_retry("prompt", {"persona": ""})
        finally:
            os.environ.pop("CRITIC_CMD", None)
        self.assertEqual(v["verdict"], "ERROR")


class TestMalformedVisibility(unittest.TestCase):
    """Task 5: a malformed reply must not silently degrade into an ordinary
    PASS — render_verdict has to shout about it."""

    def test_render_verdict_warns_on_malformed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_verdict(1, "2026-01-01T00:00:00", {
                "verdict": "PASS", "malformed": "gibberish reply from the model" * 10,
            })
        text = out.getvalue()
        self.assertIn("⚠", text)
        self.assertIn("malformed", text)
        self.assertIn("gibberish reply from the model"[:80], text)

    def test_render_verdict_clean_pass_has_no_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_verdict(1, "2026-01-01T00:00:00", {"verdict": "PASS"})
        self.assertNotIn("⚠", out.getvalue())

    def test_garbage_stub_produces_malformed_record_and_console_warning(self):
        """End-to-end through judge_batch: a stubbed model that only ever
        replies with garbage must (a) land a 'malformed' record in
        suggestions.ndjsonl and (b) print the visible warning."""
        from critic.main import judge_batch
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td)
            suggestions = cc / "suggestions.ndjsonl"
            heuristics = cc / "heuristics.md"
            stub = cc / "stub.sh"
            stub.write_text("#!/bin/sh\necho 'not json and not PASS, just noise'\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            os.environ["CRITIC_CMD"] = str(stub)
            try:
                ctx = {"heuristics_path": heuristics, "suggestions_file": suggestions,
                       "persona": "", "project": "", "repo": cc, "verify": False,
                       "beat": 1, "ts": "2026-01-01T00:00:00"}
                events = [{"type": "diff", "session": None,
                           "payload": {"diff": "+x", "stat": "", "untracked": []}}]
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    judge_batch(events, ctx)
            finally:
                os.environ.pop("CRITIC_CMD", None)
            rows = [json.loads(l) for l in suggestions.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertIn("malformed", rows[0])
            self.assertIn("⚠", out.getvalue())


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


class TestSchedulerRequeueOnFailure(unittest.TestCase):
    """A judge_fn exception (bad event shape, disk error mid-write, ...) must
    not silently drop the batch even without a process crash: the failed
    batch is re-queued, ahead of anything accumulated since, and replays on
    the next dispatch — committed_offset must never advance past it in the
    meantime."""

    def test_failed_batch_requeues_and_replays_without_masking_committed_offset(self):
        calls = []
        committed = []
        should_fail = {"a": True}

        def flaky_judge(batch, ctx):
            calls.append(list(batch))
            if should_fail["a"]:
                should_fail["a"] = False
                raise RuntimeError("boom")

        s = TurnScheduler(judge_fn=flaky_judge, judge_every_beat=True,
                          on_committed=lambda off: committed.append(off))

        # Batch A dispatches at offset 10 and its judge_fn raises.
        status = s.submit([{"type": "diff", "id": "A"}], {"offset_now": 10})
        self.assertEqual(status, "dispatched")
        s.thread.join()

        # No masking: committed_offset must not advance past A's span.
        self.assertEqual(committed, [])
        # A's events are back in pending, ready to replay.
        self.assertEqual(s.pending, [{"type": "diff", "id": "A"}])

        # Next beat: batch B's event arrives and merges with re-queued A.
        status = s.submit([{"type": "diff", "id": "B"}], {"offset_now": 20})
        self.assertEqual(status, "dispatched")
        s.thread.join()

        # judge_fn ran twice: once with just A (failed), once with A+B (ok).
        self.assertEqual([len(c) for c in calls], [1, 2])
        self.assertEqual([e["id"] for e in calls[1]], ["A", "B"])  # order preserved
        # committed_offset only advances once A actually got re-judged
        # alongside B — it is never set to a value that would mask A.
        self.assertEqual(committed, [20])


class TestSessionTagging(unittest.TestCase):
    """Task 2: findings tag back to the session that produced them, so hooks
    can scope delivery instead of broadcasting every finding to every session."""

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

    def _rows(self):
        return [json.loads(l) for l in self.suggestions.read_text().splitlines()]

    def test_majority_session_tags_record(self):
        from critic.main import judge_batch
        self._set_stub("PASS")
        events = [
            {"type": "reasoning", "session": "sess-A", "payload": {"kind": "text", "text": "a"}},
            {"type": "tool_call", "session": "sess-A", "payload": {"tool": "Edit", "input": {"file_path": "x.py"}}},
            {"type": "tool_call", "session": "sess-B", "payload": {"tool": "Edit", "input": {"file_path": "y.py"}}},
            {"type": "diff", "session": None, "payload": {"diff": "+x", "stat": "", "untracked": []}},
        ]
        judge_batch(events, {**self.ctx, "beat": 1, "ts": "t"})
        self.assertEqual(self._rows()[0]["session"], "sess-A")

    def test_no_session_events_tags_none(self):
        from critic.main import judge_batch
        self._set_stub("PASS")
        events = [{"type": "diff", "session": None,
                   "payload": {"diff": "+x", "stat": "", "untracked": []}}]
        judge_batch(events, {**self.ctx, "beat": 1, "ts": "t"})
        row = self._rows()[0]
        self.assertIn("session", row)
        self.assertIsNone(row["session"])

    def test_task_review_has_no_session_tag(self):
        from critic.main import task_review
        self._set_stub("PASS")
        self.obs.write_text(json.dumps({
            "ts": "2026-01-01T00:00:00+00:00", "beat": 1, "type": "reasoning",
            "session": "sess-A", "payload": {"kind": "text", "text": "done"}}) + "\n")
        since = 0.0
        task_review(self.obs, {**self.ctx, "beat": 1, "ts": "t"}, since_epoch=since)
        row = self._rows()[0]
        self.assertNotIn("session", row)


class TestCommittedOffset(unittest.TestCase):
    """Task 6: a crash while a judgment thread is in flight must not silently
    drop the batch — committed_offset only advances once the record lands."""

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

    def _state_roundtrip(self, state):
        state_path = self.cc / "critic-state.json"
        state_path.write_text(json.dumps(
            {k: state[k] for k in ("offset", "committed_offset", "beat") if k in state}
        ), encoding="utf-8")
        return load_state(state_path)

    def test_clean_beat_advances_committed_offset_and_roundtrips(self):
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+code", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")
        scheduler = TurnScheduler(on_committed=lambda off: state.__setitem__("committed_offset", off))
        status = heartbeat(self.obs, state, scheduler, self.ctx)
        self.assertEqual(status, "dispatched")
        scheduler.thread.join()
        self.assertEqual(state["committed_offset"], state["offset"])
        loaded = self._state_roundtrip(state)
        self.assertEqual(loaded["offset"], state["offset"])
        self.assertEqual(loaded["committed_offset"], state["committed_offset"])

    def test_crash_before_append_leaves_committed_offset_behind_and_replays(self):
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+code", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")

        def boom(batch, ctx):
            raise RuntimeError("simulated crash before append")

        scheduler = TurnScheduler(
            judge_fn=boom,
            on_committed=lambda off: state.__setitem__("committed_offset", off))
        status = heartbeat(self.obs, state, scheduler, self.ctx)
        self.assertEqual(status, "dispatched")
        scheduler.thread.join()  # the exception dies in the worker thread, not here
        self.assertGreater(state["offset"], state["committed_offset"])
        self.assertFalse(self.suggestions.exists())  # nothing durably landed
        loaded = self._state_roundtrip(state)
        self.assertEqual(loaded["offset"], state["committed_offset"])  # batch replays

    def test_legacy_state_without_committed_offset_does_not_reset_offset(self):
        state_path = self.cc / "critic-state.json"
        state_path.write_text(json.dumps({"offset": 500, "beat": 3}), encoding="utf-8")
        loaded = load_state(state_path)
        self.assertEqual(loaded["offset"], 500)
        self.assertEqual(loaded["committed_offset"], 500)

    def test_tests_run_at_is_in_persisted_state_keys_and_round_trips(self):
        """Task 9: the sticky tests-run fact must survive a daemon restart —
        it lives in the same persisted-keys list main() writes on every loop."""
        from critic.main import PERSISTED_STATE_KEYS
        self.assertIn("tests_run_at", PERSISTED_STATE_KEYS)
        state = {"offset": 0, "beat": 1, "committed_offset": 0,
                 "tests_run_at": {"sess-A": "2026-01-01T00:00:00+00:00"}}
        state_path = self.cc / "critic-state.json"
        state_path.write_text(json.dumps(
            {k: state[k] for k in PERSISTED_STATE_KEYS if k in state}
        ), encoding="utf-8")
        loaded = load_state(state_path)
        self.assertEqual(loaded["tests_run_at"], {"sess-A": "2026-01-01T00:00:00+00:00"})


class TestPromptAuditCap(unittest.TestCase):
    def test_save_prompt_prunes_beyond_cap(self):
        from critic import main as cmain
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "prompts"
            old_cap = cmain.PROMPTS_KEEP
            cmain.PROMPTS_KEEP = 3
            try:
                for i in range(6):
                    cmain.save_prompt(d, f"id{i}", f"prompt {i}")
                    os.utime(d / f"id{i}.txt", (i, i))
                cmain.save_prompt(d, "id6", "prompt 6")
                names = sorted(p.name for p in d.glob("*.txt"))
                self.assertEqual(len(names), 3)
                self.assertIn("id6.txt", names)
            finally:
                cmain.PROMPTS_KEEP = old_cap

    def test_save_case_material_prunes_beyond_cap(self):
        from critic import main as cmain
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td)
            old_cap = cmain.CASE_MATERIAL_KEEP
            cmain.CASE_MATERIAL_KEEP = 3
            try:
                for i in range(6):
                    cmain.save_case_material(cc, f"id{i}", [{"n": i}], None)
                    os.utime(cc / "case-material" / f"id{i}.json", (i, i))
                cmain.save_case_material(cc, "id6", [{"n": 6}], None)
                names = sorted(p.name for p in (cc / "case-material").glob("*.json"))
                self.assertEqual(len(names), 3)
                self.assertIn("id6.json", names)
            finally:
                cmain.CASE_MATERIAL_KEEP = old_cap

    def test_save_case_material_skips_when_too_large(self):
        from critic import main as cmain
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td)
            huge_events = [{"payload": {"text": "x" * 1000}} for _ in range(500)]
            cmain.save_case_material(cc, "big", huge_events, None)
            self.assertFalse((cc / "case-material" / "big.json").exists())

    def test_save_case_material_round_trips_events_and_diff(self):
        from critic import main as cmain
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td)
            events = [{"type": "diff", "payload": {"diff": "+x = 1"}}]
            diff = {"type": "diff", "payload": {"diff": "+x = 1"}}
            cmain.save_case_material(cc, "abc123", events, diff)
            saved = json.loads((cc / "case-material" / "abc123.json").read_text())
            self.assertEqual(saved["events"], events)
            self.assertEqual(saved["latest_diff"], diff)


class TestJudgeBatchCaseMaterial(unittest.TestCase):
    """judge_batch must freeze the exact batch inputs behind a SUGGESTION
    verdict, so the reflector can later harvest it into a frozen eval case."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
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

    def test_suggestion_verdict_writes_case_material(self):
        from critic.main import judge_batch

        self._set_stub('{"file": "x.py", "line": 2, "severity": "low", '
                       '"issue": "bug", "rationale": "r"}')
        ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
               "persona": "", "project": "", "repo": self.cc, "verify": False,
               "beat": 1, "ts": "2026-01-01T00:00:00",
               "latest_diff": {"type": "diff", "payload": {"diff": "+bad"}}}
        events = [{"type": "diff", "session": None,
                   "payload": {"diff": "+bad", "stat": "", "untracked": []}}]
        judge_batch(events, ctx)
        rows = [json.loads(l) for l in self.suggestions.read_text().splitlines()]
        verdict_id = rows[0]["id"]
        material_path = self.cc / "case-material" / f"{verdict_id}.json"
        self.assertTrue(material_path.exists())
        material = json.loads(material_path.read_text())
        self.assertEqual(material["events"], events)
        self.assertEqual(material["latest_diff"], ctx["latest_diff"])

    def test_pass_verdict_writes_no_case_material(self):
        from critic.main import judge_batch

        self._set_stub("PASS")
        ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
               "persona": "", "project": "", "repo": self.cc, "verify": False,
               "beat": 1, "ts": "2026-01-01T00:00:00"}
        events = [{"type": "diff", "session": None,
                   "payload": {"diff": "+ok", "stat": "", "untracked": []}}]
        judge_batch(events, ctx)
        self.assertFalse((self.cc / "case-material").exists())


if __name__ == "__main__":
    unittest.main()
