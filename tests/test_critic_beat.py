"""Critic tests: the heartbeat/scheduler loop -- batching, gating, session
tagging, committed offsets, and audit-trail caps."""

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

from critic.main import TurnScheduler, heartbeat, load_state, verdict_history


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

    def test_non_dict_and_garbage_observation_lines_are_skipped_not_fatal(self):
        # "skip unparseable lines rather than crash" also covers a valid-JSON
        # non-dict line (a bare scalar / list) and a typeless dict — e["type"]
        # would otherwise TypeError/KeyError and crash-loop the daemon.
        self._set_stub("PASS")
        with self.obs.open("a") as f:
            f.write("42\n")                 # valid JSON, not a dict
            f.write('["a","b"]\n')          # valid JSON list
            f.write('{"no":"type"}\n')      # dict without "type"
            f.write("not json at all\n")    # unparseable
            f.write(json.dumps({"ts": "t", "beat": 1, "type": "diff", "session": None,
                                "payload": {"diff": "+x", "stat": "", "untracked": []}}) + "\n")
        state = load_state(self.cc / "nope.json")
        scheduler = TurnScheduler()
        self.assertEqual(self._beat(state, scheduler), "dispatched")  # did not crash

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
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
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
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
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
        from hooks import logic
        import time
        self._set_stub("PASS")
        self._write_obs([
            {"ts": "t", "beat": 1, "type": "diff", "session": None,
             "payload": {"diff": "+code", "stat": "", "untracked": []}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
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
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
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
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
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


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat()


NOW = datetime.now().timestamp()


class TestReviewedFiles(unittest.TestCase):
    """Task 1: every verdict row records which files it covered (from
    latest_diff), and every non-ERROR verdict — PASS included — gets its
    case material saved so a missed PASS can later become an eval case."""

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

    def test_every_verdict_records_reviewed_files(self):
        # stub replies PASS; diff event carries touched_contents + untracked
        self._set_stub("PASS")
        self._write_obs([
            {"ts": _iso(NOW), "beat": 1, "type": "diff", "session": "s", "payload": {
                "diff": "--- a/a.py\n+++ b/a.py\n+x=1\n",
                "untracked": ["new.py"],
                "touched_contents": {"a.py": "x=1"}}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        row = json.loads(self.suggestions.read_text().splitlines()[-1])
        self.assertEqual(row["reviewed_files"], ["a.py", "new.py"])

    def test_commit_only_batch_records_reviewed_files(self):
        # a file written-and-committed within one beat carries no "diff"
        # event (only a "commit" event) — its path must still land in
        # reviewed_files so it isn't invisible to miss detection
        self._set_stub("PASS")
        self._write_obs([
            {"ts": _iso(NOW), "beat": 1, "type": "commit", "session": "s", "payload": {
                "from": "aaa", "to": "bbb", "subjects": ["add feature"],
                "diff": "--- a/committed.py\n+++ b/committed.py\n+x=1\n", "stat": ""}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        row = json.loads(self.suggestions.read_text().splitlines()[-1])
        self.assertIn("committed.py", row["reviewed_files"])

    def test_pass_verdict_saves_case_material(self):
        # same beat as above; PASS row id must have case-material JSON on disk
        self._set_stub("PASS")
        self._write_obs([
            {"ts": _iso(NOW), "beat": 1, "type": "diff", "session": "s", "payload": {
                "diff": "--- a/a.py\n+++ b/a.py\n+x=1\n",
                "untracked": ["new.py"],
                "touched_contents": {"a.py": "x=1"}}},
        ])
        state = load_state(self.cc / "nope.json")
        self._beat(state, TurnScheduler())
        row = json.loads(self.suggestions.read_text().splitlines()[-1])
        self.assertEqual(row["verdict"], "PASS")
        material = self.cc / "case-material" / f"{row['id']}.json"
        self.assertTrue(material.exists())
        data = json.loads(material.read_text())
        self.assertIn("events", data)
        self.assertIn("latest_diff", data)


class TestJudgeToolsPlumbing(unittest.TestCase):
    """Task 4: judgment turns get read-only repo tools so the model can check
    a suspicion before flagging; eval replays (no repo in ctx) stay tool-less
    so frozen-case scoring remains hermetic."""

    def test_constant_is_read_only(self):
        from critic.main import JUDGE_TOOLS
        self.assertEqual(JUDGE_TOOLS, "repo_read,repo_grep,repo_find,repo_ls")
        self.assertNotIn("bash", JUDGE_TOOLS.split(","))
        self.assertNotIn("edit", JUDGE_TOOLS.split(","))
        self.assertNotIn("write", JUDGE_TOOLS.split(","))
        # Distinct names from pi's builtins by construction — the allowlist
        # can never accidentally select a path-unsafe builtin of the same
        # short name (builtin "read" resolves absolute/~ paths; "repo_read"
        # is jailed). See critic/pi_extensions/repo_tools.mjs.
        for name in JUDGE_TOOLS.split(","):
            self.assertNotIn(name, ("read", "grep", "find", "ls"))

    def test_judgment_turn_passes_repo_tools(self):
        from critic import main as main_mod
        captured = {}

        def fake_ask(text, system=None, tools=None, cwd=None, model=None):
            captured["tools"] = tools
            captured["cwd"] = cwd
            return "PASS"

        with tempfile.TemporaryDirectory() as td:
            cc = Path(td)
            obs = cc / "observations.ndjsonl"
            suggestions = cc / "suggestions.ndjsonl"
            heuristics = cc / "heuristics.md"
            with obs.open("a") as f:
                f.write(json.dumps({
                    "ts": "t", "beat": 1, "type": "diff", "session": None,
                    "payload": {"diff": "+code", "stat": "", "untracked": []},
                }) + "\n")
            ctx = {"heuristics_path": heuristics, "suggestions_file": suggestions,
                   "persona": "", "project": "", "repo": cc, "verify": False}
            state = load_state(cc / "nope.json")
            scheduler = TurnScheduler()
            with mock.patch.object(main_mod.agent, "ask", fake_ask):
                heartbeat(obs, state, scheduler, ctx)
                if scheduler.thread:
                    scheduler.thread.join()

        self.assertEqual(captured["tools"], main_mod.JUDGE_TOOLS)
        self.assertEqual(captured["cwd"], str(cc))

    def test_eval_scoring_stays_toolless(self):
        from critic import agent as agent_mod
        from evals.run import score_heuristics
        captured = {}

        def fake_ask(prompt, system=None, tools=None, cwd=None):
            captured["tools"] = tools
            captured["cwd"] = cwd
            return "PASS"

        with mock.patch.object(agent_mod, "ask", fake_ask):
            score_heuristics("version: 1\n\n- rule\n", cases=[{
                "name": "c1", "events": [], "expected": "pass", "expect_files": [],
            }])

        self.assertIsNone(captured["tools"])
        self.assertIsNone(captured["cwd"])


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


class TestSchedulerPoisonBatchDrop(unittest.TestCase):
    """A batch whose judge_fn keeps raising (a genuinely poisoned batch, not
    a transient failure) must not be re-queued forever — that's unbounded
    memory growth and an event stream that never advances. After
    MAX_BATCH_RETRIES failed dispatches of the same batch, it's dropped with
    a loud warning instead of re-queued again."""

    def test_batch_dropped_after_max_retries_with_warning_and_committed_offset_untouched(self):
        import io
        from contextlib import redirect_stdout

        from critic.main import MAX_BATCH_RETRIES, TurnScheduler

        calls = []
        committed = []

        def always_fail(batch, ctx):
            calls.append(list(batch))
            raise RuntimeError("boom")

        s = TurnScheduler(judge_fn=always_fail, judge_every_beat=True,
                          on_committed=lambda off: committed.append(off))

        buf = io.StringIO()
        with redirect_stdout(buf):
            status = s.submit([{"type": "diff", "id": "A"}], {"offset_now": 10})
            self.assertEqual(status, "dispatched")
            s.thread.join()
            self.assertEqual(s.pending, [{"type": "diff", "id": "A"}])  # requeued (1/3)

            for _ in range(MAX_BATCH_RETRIES - 2):
                # redispatch the same requeued batch — nothing new merges in
                status = s.submit([], {"offset_now": 10})
                self.assertEqual(status, "dispatched")
                s.thread.join()
                # still under the limit: requeued, not dropped
                self.assertEqual(s.pending, [{"type": "diff", "id": "A"}])

            # final failing dispatch hits the limit and drops the batch
            status = s.submit([], {"offset_now": 10})
            self.assertEqual(status, "dispatched")
            s.thread.join()

        self.assertEqual(len(calls), MAX_BATCH_RETRIES)
        self.assertEqual(s.pending, [])  # dropped, not requeued
        self.assertEqual(committed, [])  # never advanced — a restart will replay this span once
        self.assertIn("critic: batch dropped after", buf.getvalue())
        self.assertIn("events lost: 1", buf.getvalue())

    def test_retry_count_resets_after_a_successful_dispatch(self):
        """Only one batch can be failing at a time given serialization, so a
        single scheduler-wide counter is enough — but it must reset once a
        batch actually succeeds, or a later unrelated failure would be
        dropped too early."""
        from critic.main import MAX_BATCH_RETRIES, TurnScheduler

        outcomes = iter([Exception("boom"), Exception("boom"), None, Exception("boom")])

        def flaky(batch, ctx):
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome

        s = TurnScheduler(judge_fn=flaky, judge_every_beat=True)

        s.submit([{"type": "diff"}], {})  # failure 1/3
        s.thread.join()
        self.assertEqual(s.pending, [{"type": "diff"}])
        s.submit([], {})  # redispatch same batch: failure 2/3 — still under MAX_BATCH_RETRIES
        s.thread.join()
        self.assertEqual(s.pending, [{"type": "diff"}])

        s.submit([], {})  # third dispatch succeeds -> resets the counter
        s.thread.join()
        self.assertEqual(s.pending, [])

        # a fresh failure afterwards must count as attempt 1, not 4 — so it
        # is requeued rather than dropped even though MAX_BATCH_RETRIES == 3
        # failures have happened across the scheduler's lifetime.
        s.submit([{"type": "diff"}], {})
        s.thread.join()
        self.assertEqual(s.pending, [{"type": "diff"}])
        self.assertLess(1, MAX_BATCH_RETRIES)  # sanity: this is attempt 1 of the budget


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
        return [json.loads(line) for line in self.suggestions.read_text().splitlines()]

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

    def test_non_dict_state_rebuilds_instead_of_crashing(self):
        # Valid JSON that isn't a dict must rebuild, not TypeError on
        # state["committed_offset"] = … and crash-loop on every restart.
        state_path = self.cc / "critic-state.json"
        state_path.write_text("[1, 2, 3]", encoding="utf-8")
        loaded = load_state(state_path)
        self.assertEqual(loaded, {"offset": 0, "beat": 0, "committed_offset": 0})

    def test_dict_state_missing_required_keys_is_backfilled(self):
        state_path = self.cc / "critic-state.json"
        state_path.write_text(json.dumps({"latest_diff": None}), encoding="utf-8")
        loaded = load_state(state_path)
        self.assertEqual(loaded["offset"], 0)
        self.assertEqual(loaded["beat"], 0)
        self.assertEqual(loaded["committed_offset"], 0)

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
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
        verdict_id = rows[0]["id"]
        material_path = self.cc / "case-material" / f"{verdict_id}.json"
        self.assertTrue(material_path.exists())
        material = json.loads(material_path.read_text())
        self.assertEqual(material["events"], events)
        self.assertEqual(material["latest_diff"], ctx["latest_diff"])

    def test_pass_verdict_also_writes_case_material(self):
        """Task 1: PASS verdicts get case material too — a missed PASS needs
        its packet to become an eval case later (reflector harvest)."""
        from critic.main import judge_batch

        self._set_stub("PASS")
        ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
               "persona": "", "project": "", "repo": self.cc, "verify": False,
               "beat": 1, "ts": "2026-01-01T00:00:00"}
        events = [{"type": "diff", "session": None,
                   "payload": {"diff": "+ok", "stat": "", "untracked": []}}]
        judge_batch(events, ctx)
        rows = [json.loads(line) for line in self.suggestions.read_text().splitlines()]
        verdict_id = rows[0]["id"]
        material_path = self.cc / "case-material" / f"{verdict_id}.json"
        self.assertTrue(material_path.exists())


if __name__ == "__main__":
    unittest.main()
