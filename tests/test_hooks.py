"""Hook tests: fail-open wrapper behavior, pure decision logic, install merge."""

import contextlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks import ledger as ledger_mod
from hooks.install import install
from hooks.logic import TTL_SECONDS, decide

PEER_HOOK = Path(__file__).resolve().parents[1] / "hooks" / "peer_hook.py"

NOW = time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def suggestion(sid="s1", severity="high", ts=None, file="a.py", line=3, session=None,
              council=None, verification=None):
    row = {
        "id": sid,
        "ts": _iso(NOW if ts is None else ts),
        "beat": 1,
        "verdict": "SUGGESTION",
        "session": session,
        "suggestion": {"file": file, "line": line, "severity": severity,
                       "issue": "bug here", "rationale": "because"},
    }
    if council is not None:
        row["council"] = council
    if verification is not None:
        row["verification"] = verification
    return row


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


    def test_end_to_end_receipt_announcement_via_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td) / ".codecouncil"
            (cc / "receipts").mkdir(parents=True)
            (cc / "receipts" / "repo-20260101-000000.md").write_text("# receipt\n")
            res = self._run(json.dumps(post_tool_use(cwd=td)))
            self.assertEqual(res.returncode, 0)
            out = json.loads(res.stdout)
            ctx = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("CodeCouncil wrote a session receipt:", ctx)
            self.assertIn("repo-20260101-000000.md", ctx)
            # second run: ledger persisted, receipt not re-announced
            res2 = self._run(json.dumps(post_tool_use(cwd=td)))
            self.assertEqual(res2.stdout, "")


    def test_end_to_end_stop_blocks_on_weakened_test_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td) / ".codecouncil"
            (cc / "receipts").mkdir(parents=True)
            receipt_text = (
                "# receipt\n\n## Test integrity\n"
                "- tests: weakened — 2 assertion(s) removed, 0 added\n"
                "```test-integrity\n"
                '{"verdict": "weakened", "tests_added": 0, "tests_removed": 0, '
                '"asserts_added": 0, "asserts_removed": 2}\n'
                "```\n"
            )
            (cc / "receipts" / "repo-20260101-000000.md").write_text(receipt_text)
            ev = {"hook_event_name": "Stop", "cwd": td, "stop_hook_active": False}
            res = self._run(json.dumps(ev))
            self.assertEqual(res.returncode, 0)
            out = json.loads(res.stdout)
            self.assertEqual(out["decision"], "block")
            self.assertIn("weakened its tests", out["reason"])
            # second run: ledger persisted, not blocked again
            res2 = self._run(json.dumps(ev))
            self.assertEqual(res2.stdout, "")


class TestPeerHookLocking(unittest.TestCase):
    """PostToolUse hooks run as fresh subprocesses per event, potentially from
    concurrent Claude Code sessions on the same repo. The load->decide->save
    span on delivered.json must be wrapped in an exclusive flock so two
    interleaved hook processes can't double-deliver or lose a delivery mark."""

    def test_locked_holds_a_real_exclusive_os_lock(self):
        import fcntl

        from hooks.peer_hook import _locked

        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "delivered.lock"
            with _locked(lock_path):
                self.assertTrue(lock_path.exists())
                # a second, independent file handle on the same path must not
                # be able to grab the lock while we're holding it
                fh2 = open(lock_path, "a+")
                try:
                    with self.assertRaises(OSError):
                        fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    fh2.close()
            # once the context exits, the lock must be released
            fh3 = open(lock_path, "a+")
            try:
                fcntl.flock(fh3.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
                fcntl.flock(fh3.fileno(), fcntl.LOCK_UN)
            finally:
                fh3.close()

    def test_locked_fails_open_when_lock_path_unusable(self):
        """If acquiring the lock fails for any reason (e.g. the sidecar's
        parent can't be created), _locked must still yield rather than raise."""
        from hooks.peer_hook import _locked

        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("not a directory")
            unusable_lock = blocker / "sub" / "delivered.lock"
            entered = False
            with _locked(unusable_lock):
                entered = True
            self.assertTrue(entered)

    def test_lock_wraps_the_entire_load_decide_save_span(self):
        """The lock must cover load, decide, and save as one span — locking
        only part of it would still let two subprocesses interleave."""
        import hooks.peer_hook as peer_hook

        events = []

        @contextlib.contextmanager
        def fake_lock(path):
            events.append(("acquire", path))
            yield
            events.append(("release", path))

        orig_load, orig_save = ledger_mod.load, ledger_mod.save

        def tracking_load(path):
            events.append(("load",))
            return orig_load(path)

        def tracking_save(path, ledger):
            events.append(("save",))
            return orig_save(path, ledger)

        with tempfile.TemporaryDirectory() as td:
            cc = Path(td) / ".codecouncil"
            cc.mkdir()
            (cc / "suggestions.ndjsonl").write_text(json.dumps(suggestion()) + "\n")
            with mock.patch.object(peer_hook, "_locked", fake_lock), \
                 mock.patch.object(ledger_mod, "load", tracking_load), \
                 mock.patch.object(ledger_mod, "save", tracking_save):
                peer_hook.run(json.dumps(post_tool_use(cwd=td)))

            self.assertEqual(events, [
                ("acquire", cc / "delivered.lock"),
                ("load",),
                ("save",),
                ("release", cc / "delivered.lock"),
            ])

    def test_two_interleaved_hook_processes_deliver_exactly_once(self):
        """Two concurrent hook subprocesses racing on the same suggestion must
        result in exactly one delivery, not zero or two."""
        with tempfile.TemporaryDirectory() as td:
            cc = Path(td) / ".codecouncil"
            cc.mkdir()
            (cc / "suggestions.ndjsonl").write_text(json.dumps(suggestion()) + "\n")

            def run_hook():
                return subprocess.run(
                    [sys.executable, str(PEER_HOOK)],
                    input=json.dumps(post_tool_use(cwd=td)),
                    capture_output=True, text=True, timeout=30,
                )

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(run_hook)
                f2 = pool.submit(run_hook)
                results = [f1.result(), f2.result()]

            delivered = [r for r in results if r.stdout.strip()]
            self.assertEqual(len(delivered), 1)
            ledger = ledger_mod.load(cc / "delivered.json")
            self.assertTrue(ledger_mod.delivered(ledger, "s1", "context"))


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


class TestCouncilProberOnlyGate(unittest.TestCase):
    """Task 3: a finding whose council.agreement is "prober-only" came from
    the recall-prober alone — the precision anchor PASSed it. Per the
    measured false-positive profile such findings must reach the coding
    agent ONLY when a repro has actually verified them. "both"/"primary-only"
    rows (and rows with no "council" key at all) are unaffected."""

    # -- context channel (PostToolUse) --

    def test_prober_only_verified_delivers_context(self):
        rows = [suggestion(council={"agreement": "prober-only"},
                           verification={"status": "verified"})]
        out = decide(post_tool_use(), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_prober_only_inconclusive_silent_context(self):
        rows = [suggestion(council={"agreement": "prober-only"},
                           verification={"status": "inconclusive"})]
        out = decide(post_tool_use(), rows, {}, NOW)
        self.assertIsNone(out)

    def test_prober_only_no_verification_key_silent_context(self):
        rows = [suggestion(council={"agreement": "prober-only"})]
        out = decide(post_tool_use(), rows, {}, NOW)
        self.assertIsNone(out)

    def test_agreement_both_delivers_without_verification_context(self):
        rows = [suggestion(council={"agreement": "both"})]
        out = decide(post_tool_use(), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_agreement_primary_only_delivers_without_verification_context(self):
        rows = [suggestion(council={"agreement": "primary-only"})]
        out = decide(post_tool_use(), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_no_council_key_unchanged_context(self):
        rows = [suggestion()]
        out = decide(post_tool_use(), rows, {}, NOW)
        self.assertIsNotNone(out)

    # -- block channel (Stop) --

    def test_prober_only_verified_delivers_block(self):
        rows = [suggestion(council={"agreement": "prober-only"},
                           verification={"status": "verified"})]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNotNone(out)
        self.assertEqual(out["decision"], "block")

    def test_prober_only_inconclusive_silent_block(self):
        rows = [suggestion(council={"agreement": "prober-only"},
                           verification={"status": "inconclusive"})]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNone(out)

    def test_prober_only_no_verification_key_silent_block(self):
        rows = [suggestion(council={"agreement": "prober-only"})]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNone(out)

    def test_agreement_both_delivers_without_verification_block(self):
        rows = [suggestion(council={"agreement": "both"})]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_agreement_primary_only_delivers_without_verification_block(self):
        rows = [suggestion(council={"agreement": "primary-only"})]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNotNone(out)

    def test_no_council_key_unchanged_block(self):
        rows = [suggestion()]
        out = decide(stop_event(), rows, {}, NOW)
        self.assertIsNotNone(out)


class TestReceiptAnnouncement(unittest.TestCase):
    """Task: surface the newest unannounced session receipt into the coding
    agent's transcript. decide() takes filenames+paths as data (peer_hook.py
    does the directory listing) so it stays pure and fs-free."""

    def _receipt(self, name="sess-a-20260101-120000.md", path=None):
        return {"name": name, "path": path or f"/tmp/.codecouncil/receipts/{name}"}

    def test_receipt_announced_once_on_post_tool_use(self):
        ledger = {}
        out = decide(post_tool_use(), [], ledger, NOW, receipts=[self._receipt()])
        self.assertIsNotNone(out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CodeCouncil wrote a session receipt:", ctx)
        self.assertIn("sess-a-20260101-120000.md", ctx)
        self.assertTrue(ledger_mod.receipt_announced(ledger, "sess-a-20260101-120000.md"))
        # second call: same receipt must not be re-announced
        out2 = decide(post_tool_use(), [], ledger, NOW, receipts=[self._receipt()])
        self.assertIsNone(out2)

    def test_receipt_appended_to_existing_findings_text(self):
        rows = [suggestion()]
        ledger = {}
        out = decide(post_tool_use(), rows, ledger, NOW, receipts=[self._receipt()])
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bug here", ctx)
        self.assertIn("CodeCouncil wrote a session receipt:", ctx)

    def test_no_receipts_unchanged_output(self):
        rows = [suggestion()]
        ledger = {}
        out = decide(post_tool_use(), rows, ledger, NOW)  # default receipts=()
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("session receipt", ctx)

    def test_newest_first_only_one_announced_per_event(self):
        ledger = {}
        receipts = [self._receipt("new.md"), self._receipt("old.md")]
        out = decide(post_tool_use(), [], ledger, NOW, receipts=receipts)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new.md", ctx)
        self.assertNotIn("old.md", ctx)

    def test_user_prompt_submit_announces_receipt(self):
        ledger = {}
        out = decide({"hook_event_name": "UserPromptSubmit", "cwd": "/x"}, [], ledger, NOW,
                     receipts=[self._receipt()])
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_stop_does_not_announce_receipt(self):
        # Investigation: Claude Code's Stop hook JSON only supports
        # decision:"block"/reason — there is no additionalContext (or other
        # non-blocking text) channel for Stop, so a receipt can't be surfaced
        # there without also blocking the agent's completion. Receipts are
        # therefore announced only on PostToolUse / UserPromptSubmit; an
        # unannounced receipt just waits for the next one of those (session
        # start already does the same thing for findings).
        ledger = {}
        out = decide(stop_event(), [], ledger, NOW, receipts=[self._receipt()])
        self.assertIsNone(out)
        self.assertFalse(ledger_mod.receipt_announced(ledger, "sess-a-20260101-120000.md"))

    def test_decide_stays_pure_no_filesystem_access(self):
        ledger = {}
        with mock.patch("pathlib.Path.glob", side_effect=AssertionError("decide() touched fs")), \
             mock.patch("pathlib.Path.iterdir", side_effect=AssertionError("decide() touched fs")), \
             mock.patch("pathlib.Path.exists", side_effect=AssertionError("decide() touched fs")):
            out = decide(post_tool_use(), [], ledger, NOW, receipts=[self._receipt()])
        self.assertIsNotNone(out)


def ti_receipt(name="sess-a-20260101-120000.md", verdict="weakened", asserts_removed=3,
              asserts_added=1, tests_removed=0, tests_added=0, path=None, no_key=False):
    row = {"name": name, "path": path or f"/tmp/.codecouncil/receipts/{name}"}
    if not no_key:
        row["test_integrity"] = {
            "verdict": verdict, "asserts_removed": asserts_removed, "asserts_added": asserts_added,
            "tests_removed": tests_removed, "tests_added": tests_added,
        }
    return row


class TestDoneGateTestIntegrity(unittest.TestCase):
    """Task 2: a session's latest receipt scoring its tests "weakened" blocks
    Stop once, mirroring the existing high-severity block-once pattern but
    keyed by receipt (ledger key "test_integrity") instead of suggestion id."""

    def test_weakened_blocks_stop_once(self):
        ledger = {}
        out = decide(stop_event(), [], ledger, NOW, receipts=[ti_receipt()])
        self.assertIsNotNone(out)
        self.assertEqual(out["decision"], "block")
        self.assertIn("weakened its tests", out["reason"])
        self.assertIn("3 assertions removed", out["reason"])
        self.assertIn("1 added", out["reason"])
        self.assertIn("COUNCIL-REBUTTAL", out["reason"])

    def test_weakened_never_blocks_twice_for_same_receipt(self):
        ledger = {}
        receipts = [ti_receipt()]
        self.assertIsNotNone(decide(stop_event(), [], ledger, NOW, receipts=receipts))
        self.assertIsNone(decide(stop_event(), [], ledger, NOW, receipts=receipts))

    def test_weakened_never_blocks_twice_even_after_a_rebuttal(self):
        """decide() has no transcript access — a COUNCIL-REBUTTAL reply is
        graded by the Reflector, not parsed here. The block-once ledger mark
        IS the mechanism that respects it: once blocked, Stop never re-blocks
        for that receipt, so a session that rebuts (or fixes) and calls Stop
        again is not interrupted a second time."""
        ledger = {}
        receipts = [ti_receipt()]
        first = decide(stop_event(), [], ledger, NOW, receipts=receipts)
        self.assertEqual(first["decision"], "block")
        # simulate time passing after the agent replied with a rebuttal and
        # tried to finish again
        second = decide(stop_event(), [], ledger, NOW + 5, receipts=receipts)
        self.assertIsNone(second)

    def test_unchanged_receipt_does_not_block(self):
        out = decide(stop_event(), [], {}, NOW, receipts=[ti_receipt(verdict="unchanged")])
        self.assertIsNone(out)

    def test_strengthened_receipt_does_not_block(self):
        out = decide(stop_event(), [], {}, NOW, receipts=[ti_receipt(verdict="strengthened")])
        self.assertIsNone(out)

    def test_receipt_without_test_integrity_key_is_silent(self):
        # a receipt written before this feature existed
        out = decide(stop_event(), [], {}, NOW, receipts=[ti_receipt(no_key=True)])
        self.assertIsNone(out)

    def test_no_receipts_no_block(self):
        self.assertIsNone(decide(stop_event(), [], {}, NOW, receipts=[]))

    def test_stop_hook_active_skips_test_integrity_block(self):
        out = decide(stop_event(active=True), [], {}, NOW, receipts=[ti_receipt()])
        self.assertIsNone(out)

    def test_high_severity_suggestion_block_takes_priority_this_turn(self):
        rows = [suggestion()]
        ledger = {}
        out = decide(stop_event(), rows, ledger, NOW, receipts=[ti_receipt()])
        self.assertEqual(out["decision"], "block")
        self.assertIn("a.py:3", out["reason"])  # the suggestion block, not test-integrity
        self.assertFalse(ledger_mod.test_integrity_blocked(ledger, "sess-a-20260101-120000.md"))
        # next Stop: suggestion already delivered, so the test-integrity
        # block fires on this later call
        out2 = decide(stop_event(), rows, ledger, NOW, receipts=[ti_receipt()])
        self.assertIn("weakened its tests", out2["reason"])

    def test_only_newest_receipt_considered(self):
        receipts = [ti_receipt("new.md", verdict="unchanged"),
                   ti_receipt("old.md", verdict="weakened")]
        out = decide(stop_event(), [], {}, NOW, receipts=receipts)
        self.assertIsNone(out)

    def test_decide_stays_pure_for_test_integrity_gate(self):
        with mock.patch("pathlib.Path.glob", side_effect=AssertionError("decide() touched fs")), \
             mock.patch("pathlib.Path.read_text", side_effect=AssertionError("decide() touched fs")):
            out = decide(stop_event(), [], {}, NOW, receipts=[ti_receipt()])
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
