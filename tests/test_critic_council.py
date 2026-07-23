"""Critic tests: council mode -- merge_council, the second-model judge_batch
wiring, and prober resolution."""

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

from critic.render import render_verdict


PRIMARY_SUGGESTION = {"verdict": "SUGGESTION",
                      "suggestion": {"file": "primary.py", "line": 1,
                                    "severity": "medium", "issue": "primary issue",
                                    "rationale": "", "rule": None, "failure_mode": None}}


PROBER_SUGGESTION = {"verdict": "SUGGESTION",
                     "suggestion": {"file": "prober.py", "line": 2,
                                   "severity": "high", "issue": "prober issue",
                                   "rationale": "", "rule": None, "failure_mode": None}}


PRIMARY_PASS = {"verdict": "PASS"}


PROBER_PASS = {"verdict": "PASS"}


PRIMARY_ERROR = {"verdict": "ERROR", "error": "boom"}


PROBER_ERROR = {"verdict": "ERROR", "error": "boom"}


class TestMergeCouncil(unittest.TestCase):
    """Task 2: merge_council is pure (no I/O, no model calls) so all six
    combos from the brief's table are unit-testable directly."""

    def test_both_suggest_primary_wins_agreement_both(self):
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_SUGGESTION, PROBER_SUGGESTION)
        self.assertEqual(chosen, PRIMARY_SUGGESTION)
        self.assertEqual(council["agreement"], "both")
        self.assertEqual(council["prober_verdict"], "SUGGESTION")

    def test_primary_suggests_prober_passes_agreement_primary_only(self):
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_SUGGESTION, PROBER_PASS)
        self.assertEqual(chosen, PRIMARY_SUGGESTION)
        self.assertEqual(council["agreement"], "primary-only")
        self.assertEqual(council["prober_verdict"], "PASS")

    def test_primary_suggests_prober_errors_agreement_primary_only(self):
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_SUGGESTION, PROBER_ERROR)
        self.assertEqual(chosen, PRIMARY_SUGGESTION)
        self.assertEqual(council["agreement"], "primary-only")
        self.assertEqual(council["prober_verdict"], "ERROR")

    def test_primary_passes_prober_suggests_agreement_prober_only(self):
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_PASS, PROBER_SUGGESTION)
        self.assertEqual(chosen, PROBER_SUGGESTION)
        self.assertEqual(council["agreement"], "prober-only")
        self.assertEqual(council["prober_verdict"], "SUGGESTION")

    def test_both_pass_agreement_both(self):
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_PASS, PROBER_PASS)
        self.assertEqual(chosen, PRIMARY_PASS)
        self.assertEqual(council["agreement"], "both")
        self.assertEqual(council["prober_verdict"], "PASS")

    def test_primary_passes_prober_errors_agreement_primary_only(self):
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_PASS, PROBER_ERROR)
        self.assertEqual(chosen, PRIMARY_PASS)
        self.assertEqual(council["agreement"], "primary-only")
        self.assertEqual(council["prober_verdict"], "ERROR")

    def test_primary_errors_prober_suggests_agreement_prober_only(self):
        """Safety-edge case flagged in review: a primary AgentError must not
        swallow a prober catch. The prober's suggestion becomes the chosen
        verdict AND agreement is tagged "prober-only" — this exact tag is
        what Task 3's verification/delivery gate keys off of to require
        repro proof before ever delivering a prober-only finding. If this
        combo were mis-tagged "both" or "primary-only" here, Task 3's gate
        would either skip verification it must run, or never run at all."""
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_ERROR, PROBER_SUGGESTION)
        self.assertEqual(chosen, PROBER_SUGGESTION)
        self.assertEqual(council["agreement"], "prober-only")
        self.assertEqual(council["prober_verdict"], "SUGGESTION")

    def test_primary_errors_prober_passes_verdict_stays_error(self):
        """Safety-edge case: when the primary itself failed and the prober
        found nothing, the merged verdict must stay "ERROR" (judge_batch's
        render_error/ERROR-row branch depends on this), not silently become
        a clean PASS. merge_council's agreement label here is "both" —
        cosmetically odd for an ERROR row, but harmless: ERROR short-circuits
        judge_batch before any delivery-eligibility check ever reads
        council["agreement"], so no gate can be fooled by the label. Only the
        verdict value is load-bearing for this combo."""
        from critic.main import merge_council
        chosen, council = merge_council(PRIMARY_ERROR, PROBER_PASS)
        self.assertEqual(chosen["verdict"], "ERROR")
        self.assertEqual(council["agreement"], "both")
        self.assertEqual(council["prober_verdict"], "PASS")


COUNCIL_PRIMARY_PASS_PROBER_SUGGESTS = """#!/usr/bin/env python3
import sys
from pathlib import Path
calls_log = Path(__file__).parent / "calls.log"
model = sys.argv[2] if len(sys.argv) > 2 else ""
with calls_log.open("a") as f:
    f.write(model + "\\n")
if model == "prober/model":
    print('{"file": "found-by-prober.py", "line": 3, "issue": "recall catch", "severity": "high"}')
else:
    print("PASS")
"""


COUNCIL_ONLY_ONE_CALL_EXPECTED = """#!/usr/bin/env python3
import sys
from pathlib import Path
calls_log = Path(__file__).parent / "calls.log"
model = sys.argv[2] if len(sys.argv) > 2 else ""
with calls_log.open("a") as f:
    f.write(model + "\\n")
print("PASS")
"""


COUNCIL_PROBER_ERRORS = """#!/usr/bin/env python3
import sys
from pathlib import Path
calls_log = Path(__file__).parent / "calls.log"
model = sys.argv[2] if len(sys.argv) > 2 else ""
with calls_log.open("a") as f:
    f.write(model + "\\n")
if model == "prober/model":
    sys.exit(1)  # simulates an AgentError (stub failed)
print("PASS")
"""


class TestCouncilJudgeBatch(unittest.TestCase):
    """Task 2: judge_batch wires ctx['prober'] through to a second ask_with_retry
    call and merges the two verdicts via merge_council."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
        self.suggestions = self.cc / "suggestions.ndjsonl"
        self.heuristics = self.cc / "heuristics.md"
        self.calls_log = self.cc / "calls.log"
        self.events = [{"type": "diff", "session": None,
                        "payload": {"diff": "+code", "stat": "", "untracked": []}}]
        self.base_ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
                         "persona": "", "project": "", "repo": self.cc,
                         "beat": 1, "ts": "2026-01-01T00:00:00"}

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def _set_stub(self, script: str):
        stub = self.cc / "stub.py"
        stub.write_text(script)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        os.environ["CRITIC_CMD"] = str(stub)

    def _rows(self):
        return [json.loads(line) for line in self.suggestions.read_text().splitlines()]

    def _calls(self):
        if not self.calls_log.exists():
            return []
        return [line for line in self.calls_log.read_text().splitlines() if line or line == ""]

    def test_prober_only_finding_gets_council_agreement_and_survives(self):
        from critic.main import judge_batch
        self._set_stub(COUNCIL_PRIMARY_PASS_PROBER_SUGGESTS)
        # verify True (the default): a prober is only consulted when
        # verification is possible — see test_no_verify_skips_prober below.
        # The flagged file doesn't exist under self.cc, so verify_finding
        # short-circuits on its own "file not found" check without an extra
        # model call, keeping the call count at exactly 2 (primary + prober).
        ctx = {**self.base_ctx, "verify": True, "prober": "prober/model"}
        judge_batch(self.events, ctx)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["verdict"], "SUGGESTION")
        self.assertEqual(row["suggestion"]["issue"], "recall catch")
        self.assertIn("council", row)
        self.assertEqual(row["council"]["agreement"], "prober-only")
        self.assertEqual(row["council"]["prober_verdict"], "SUGGESTION")
        self.assertEqual(row["council"]["prober_model"], "prober/model")
        calls = self._calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("prober/model", calls)

    def test_prober_agent_error_never_blocks_primary_verdict(self):
        from critic.main import judge_batch
        self._set_stub(COUNCIL_PROBER_ERRORS)
        ctx = {**self.base_ctx, "verify": True, "prober": "prober/model"}
        judge_batch(self.events, ctx)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["verdict"], "PASS")
        self.assertIn("council", row)
        self.assertEqual(row["council"]["prober_verdict"], "ERROR")
        self.assertEqual(row["council"]["agreement"], "primary-only")

    def test_no_prober_configured_no_council_key_single_call(self):
        from critic.main import judge_batch
        self._set_stub(COUNCIL_ONLY_ONE_CALL_EXPECTED)
        ctx = {**self.base_ctx, "verify": False}  # no "prober" key at all
        judge_batch(self.events, ctx)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotIn("council", row)
        self.assertEqual(len(self._calls()), 1)

    def test_no_verify_skips_prober_even_when_configured(self):
        """A prober without a verifier is a false-positive machine (bake-off
        data) — --no-verify must suppress the prober call entirely, not just
        the verification step."""
        from critic.main import judge_batch
        self._set_stub(COUNCIL_ONLY_ONE_CALL_EXPECTED)
        ctx = {**self.base_ctx, "verify": False, "prober": "prober/model"}
        judge_batch(self.events, ctx)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertNotIn("council", row)
        self.assertEqual(len(self._calls()), 1)


class TestCouncilRenderRegression(unittest.TestCase):
    """Task 2 review follow-up: render_verdict must not raise on a
    council-bearing record, on either the PASS or the SUGGESTION branch, and
    must surface the council note text in both cases."""

    def test_render_verdict_pass_with_council_both_agreement(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_verdict(1, "2026-01-01T00:00:00", {
                "verdict": "PASS",
                "council": {"prober_model": "x", "prober_verdict": "PASS", "agreement": "both"},
            })
        text = out.getvalue()
        self.assertIn("council: prober agreed", text)

    def test_render_verdict_suggestion_with_council_prober_only_agreement(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            render_verdict(1, "2026-01-01T00:00:00", {
                "verdict": "SUGGESTION",
                "suggestion": {"file": "x.py", "line": 1, "severity": "medium",
                               "issue": "possible bug", "rationale": ""},
                "council": {"prober_model": "x", "prober_verdict": "SUGGESTION",
                           "agreement": "prober-only"},
            })
        text = out.getvalue()
        self.assertIn("council: prober-only finding (needs proof)", text)


class TestResolveProber(unittest.TestCase):
    """Task 4: council mode's model precedence for ctx["prober"] —
    --prober flag > COUNCIL_PROBER env > None. Testing the extracted pure
    helper (rather than critic.main.main(), which has no existing direct-call
    test pattern — it blocks on wait_for/the daemon loop) keeps this
    RED/GREEN cheap and matches how the rest of this module is tested."""

    def test_flag_beats_env(self):
        from critic.main import resolve_prober
        self.assertEqual(
            resolve_prober("openrouter/openai/gpt-5-mini", {"COUNCIL_PROBER": "other/model"}),
            "openrouter/openai/gpt-5-mini")

    def test_env_used_when_no_flag(self):
        from critic.main import resolve_prober
        self.assertEqual(
            resolve_prober(None, {"COUNCIL_PROBER": "openrouter/openai/gpt-5-mini"}),
            "openrouter/openai/gpt-5-mini")

    def test_none_when_neither_set(self):
        from critic.main import resolve_prober
        self.assertIsNone(resolve_prober(None, {}))


class TestMainResolvesProberFromLocalEnv(unittest.TestCase):
    """Regression (reviewer catch on T4): main() must resolve COUNCIL_PROBER
    from agent.local_env() (which tops up from ~/.codecouncil/env), not raw
    os.environ — otherwise a COUNCIL_PROBER set only in that file passes
    codecouncil's preflight check (which already reads local_env()) but
    silently never activates council mode. Monkeypatches agent.local_env and
    drives main()'s own --once path end to end, the way the module's other
    integration-style tests (TestHeartbeatWithStub) exercise real code paths
    rather than re-testing resolve_prober's precedence in isolation."""

    def test_council_prober_from_local_env_file_activates(self):
        import critic.main as main_mod
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            cc = repo / ".codecouncil"
            cc.mkdir()
            (cc / "observations.ndjsonl").write_text("")
            with mock.patch.object(
                main_mod.agent, "local_env",
                return_value={"COUNCIL_PROBER": "openrouter/openai/gpt-5-mini"}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = main_mod.main([str(repo), "--once"])
            self.assertEqual(rc, 0)
            self.assertIn("critic: council mode — prober openrouter/openai/gpt-5-mini",
                          out.getvalue())


if __name__ == "__main__":
    unittest.main()
