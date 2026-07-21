"""Hook tests: fail-open wrapper behavior, pure decision logic, install merge."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks import ledger as ledger_mod
from hooks.install import install
from hooks.logic import TTL_SECONDS, decide

PEER_HOOK = Path(__file__).resolve().parents[1] / "hooks" / "peer_hook.py"

NOW = time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def suggestion(sid="s1", severity="high", ts=None, file="a.py", line=3, session=None):
    return {
        "id": sid,
        "ts": _iso(NOW if ts is None else ts),
        "beat": 1,
        "verdict": "SUGGESTION",
        "session": session,
        "suggestion": {"file": file, "line": line, "severity": severity,
                       "issue": "bug here", "rationale": "because"},
    }


def post_tool_use(cwd="/tmp", session_id=None):
    ev = {"hook_event_name": "PostToolUse", "cwd": cwd, "tool_name": "Edit"}
    if session_id is not None:
        ev["session_id"] = session_id
    return ev


def stop_event(cwd="/tmp", active=False, session_id=None):
    ev = {"hook_event_name": "Stop", "cwd": cwd, "stop_hook_active": active}
    if session_id is not None:
        ev["session_id"] = session_id
    return ev


class TestFailOpen(unittest.TestCase):
    """The hook must exit 0 with no output no matter what it is fed."""

    def _run(self, stdin: str):
        return subprocess.run([sys.executable, str(PEER_HOOK)], input=stdin,
                              capture_output=True, text=True, timeout=30)

    def test_garbage_stdin(self):
        for bad in ("", "not json", "[]", '{"no": "cwd"}'):
            res = self._run(bad)
            self.assertEqual(res.returncode, 0, bad)
            self.assertEqual(res.stdout, "", bad)

    def test_missing_codecouncil_dir_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            res = self._run(json.dumps(post_tool_use(cwd=td)))
            self.assertEqual((res.returncode, res.stdout), (0, ""))

    def test_stop_writes_review_request(self):
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td) / ".codecouncil"
            cc.mkdir()
            (cc / "suggestions.ndjsonl").write_text("")
            ev = {"hook_event_name": "Stop", "cwd": td, "stop_hook_active": False,
                  "session_id": "sess-42"}
            res = self._run(json.dumps(ev))
            self.assertEqual((res.returncode, res.stdout), (0, ""))  # no block: nothing pending
            reqs = (cc / "review-requests.ndjsonl").read_text().splitlines()
            self.assertEqual(len(reqs), 1)
            self.assertEqual(json.loads(reqs[0])["session"], "sess-42")

    def test_end_to_end_injection_via_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td) / ".codecouncil"
            cc.mkdir()
            (cc / "suggestions.ndjsonl").write_text(json.dumps(suggestion()) + "\n")
            res = self._run(json.dumps(post_tool_use(cwd=td)))
            self.assertEqual(res.returncode, 0)
            out = json.loads(res.stdout)
            self.assertIn("bug here", out["hookSpecificOutput"]["additionalContext"])
            # second run: ledger persisted, nothing delivered again
            res2 = self._run(json.dumps(post_tool_use(cwd=td)))
            self.assertEqual(res2.stdout, "")


class TestDecideContext(unittest.TestCase):
    def test_medium_and_high_injected_low_ignored(self):
        rows = [suggestion("a", "low"), suggestion("b", "medium"), suggestion("c", "high")]
        ledger = {}
        out = decide(post_tool_use(), rows, ledger, NOW)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[MEDIUM]", ctx)
        self.assertIn("[HIGH]", ctx)
        self.assertNotIn("[LOW]", ctx)
        self.assertTrue(ledger_mod.delivered(ledger, "b", "context"))

    def test_delivered_once(self):
        rows = [suggestion()]
        ledger = {}
        self.assertIsNotNone(decide(post_tool_use(), rows, ledger, NOW))
        self.assertIsNone(decide(post_tool_use(), rows, ledger, NOW))

    def test_ttl_expired_never_delivered(self):
        rows = [suggestion(ts=NOW - TTL_SECONDS - 5)]
        self.assertIsNone(decide(post_tool_use(), rows, {}, NOW))

    def test_pass_and_idless_rows_ignored(self):
        rows = [{"verdict": "PASS", "ts": _iso(NOW)},
                {**suggestion(), "id": None}]
        self.assertIsNone(decide(post_tool_use(), rows, {}, NOW))

    def test_context_capped_at_three(self):
        rows = [suggestion(sid=f"s{i}") for i in range(5)]
        ledger = {}
        out = decide(post_tool_use(), rows, ledger, NOW)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"].count("[HIGH]"), 3)
        self.assertEqual(sum(1 for i in range(5) if ledger_mod.delivered(ledger, f"s{i}", "context")), 3)


class TestDecideSessionStart(unittest.TestCase):
    def test_user_prompt_submit_delivers_and_shares_ledger_channel(self):
        rows = [suggestion()]
        ledger = {}
        out = decide({"hook_event_name": "UserPromptSubmit", "cwd": "/x"}, rows, ledger, NOW)
        self.assertIn("bug here", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        # same context channel: a later PostToolUse must not re-deliver
        self.assertIsNone(decide(post_tool_use(), rows, ledger, NOW))


class TestDecideStop(unittest.TestCase):
    def test_high_blocks_once(self):
        rows = [suggestion()]
        ledger = {}
        out = decide(stop_event(), rows, ledger, NOW)
        self.assertEqual(out["decision"], "block")
        self.assertIn("a.py:3", out["reason"])
        self.assertIsNone(decide(stop_event(), rows, ledger, NOW))

    def test_medium_never_blocks(self):
        self.assertIsNone(decide(stop_event(), [suggestion(severity="medium")], {}, NOW))

    def test_stop_hook_active_always_allows(self):
        self.assertIsNone(decide(stop_event(active=True), [suggestion()], {}, NOW))

    def test_one_block_per_stop(self):
        rows = [suggestion("x"), suggestion("y")]
        ledger = {}
        decide(stop_event(), rows, ledger, NOW)
        blocked = [s for s in ("x", "y") if ledger_mod.delivered(ledger, s, "block")]
        self.assertEqual(len(blocked), 1)

    def test_context_delivery_does_not_prevent_block(self):
        rows = [suggestion()]
        ledger = {}
        decide(post_tool_use(), rows, ledger, NOW)
        self.assertIsNotNone(decide(stop_event(), rows, ledger, NOW))


class TestSessionScopedDelivery(unittest.TestCase):
    """Task 2: a finding tagged with the session that produced it must not
    leak into an unrelated session's context/block channel."""

    # -- PostToolUse / context channel --

    def test_matching_session_delivered_context(self):
        rows = [suggestion(session="sess-A")]
        out = decide(post_tool_use(session_id="sess-A"), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_mismatched_session_skipped_context(self):
        rows = [suggestion(session="sess-A")]
        out = decide(post_tool_use(session_id="sess-B"), rows, {}, NOW)
        self.assertIsNone(out)

    def test_repo_wide_delivered_regardless_of_session_context(self):
        rows = [suggestion()]  # no session tag: e.g. a task review
        out = decide(post_tool_use(session_id="sess-B"), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_tagged_row_delivered_when_event_lacks_session_id_context(self):
        rows = [suggestion(session="sess-A")]
        out = decide(post_tool_use(), rows, {}, NOW)  # hook event has no session_id
        self.assertIsNotNone(out)

    # -- Stop / block channel --

    def test_matching_session_delivered_block(self):
        rows = [suggestion(session="sess-A")]
        out = decide(stop_event(session_id="sess-A"), rows, {}, NOW)
        self.assertEqual(out["decision"], "block")

    def test_mismatched_session_skipped_block(self):
        rows = [suggestion(session="sess-A")]
        out = decide(stop_event(session_id="sess-B"), rows, {}, NOW)
        self.assertIsNone(out)

    def test_repo_wide_delivered_regardless_of_session_block(self):
        rows = [suggestion()]
        out = decide(stop_event(session_id="sess-B"), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_tagged_row_delivered_when_event_lacks_session_id_block(self):
        rows = [suggestion(session="sess-A")]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNotNone(out)


class TestInstall(unittest.TestCase):
    def test_fresh_install_then_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self.assertEqual(install(repo), ["PostToolUse", "Stop", "UserPromptSubmit"])
            settings = json.loads((repo / ".claude" / "settings.json").read_text())
            self.assertEqual(settings["hooks"]["PostToolUse"][0]["matcher"],
                             "Edit|Write|MultiEdit|NotebookEdit")
            self.assertEqual(install(repo), [])  # second run: no-op

    def test_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".claude").mkdir()
            (repo / ".claude" / "settings.json").write_text(json.dumps({
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other.sh"}]}]},
            }))
            install(repo)
            settings = json.loads((repo / ".claude" / "settings.json").read_text())
            self.assertEqual(settings["permissions"]["allow"], ["Bash(ls:*)"])
            cmds = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
            self.assertEqual(len(cmds), 2)
            self.assertIn("other.sh", cmds)


if __name__ == "__main__":
    unittest.main()
