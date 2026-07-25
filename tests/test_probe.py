"""Property probes: candidates() extraction, budgeted run_probes(), and the
--probes/COUNCIL_PROBES wiring into critic.main.judge_batch."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from critic import probe


def _diff(*files_and_blocks):
    """Build a minimal unified diff with one or more files, each a single
    block of consecutive '+' lines (a fully-added function)."""
    out = []
    for fname, added_lines in files_and_blocks:
        out += [f"diff --git a/{fname} b/{fname}", "--- /dev/null", f"+++ b/{fname}",
                "@@ -0,0 +1,50 @@"]
        out += [f"+{line}" if line else "+" for line in added_lines.splitlines()]
    return "\n".join(out)


DOCSTRING_FUNC = '''def parse_version(v):
    """Parse a version string. Returns None if the input is invalid."""
    if not v:
        raise ValueError("empty version")
    return v.split(".")
'''

NO_DOCSTRING_FUNC = '''def helper(x):
    return x + 1
'''


class TestCandidates(unittest.TestCase):
    def test_extracts_function_with_docstring(self):
        diff = _diff(("mod.py", DOCSTRING_FUNC))
        cands = probe.candidates(diff)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["file"], "mod.py")
        self.assertEqual(cands[0]["qualname"], "parse_version")
        self.assertIn("Returns None if the input is invalid", cands[0]["promise"])

    def test_function_without_docstring_is_skipped(self):
        diff = _diff(("mod.py", NO_DOCSTRING_FUNC))
        cands = probe.candidates(diff)
        self.assertEqual(cands, [])

    def test_mixed_functions_only_docstring_one_extracted(self):
        diff = _diff(("mod.py", DOCSTRING_FUNC + "\n" + NO_DOCSTRING_FUNC))
        cands = probe.candidates(diff)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["qualname"], "parse_version")

    def test_non_python_file_ignored(self):
        diff = _diff(("readme.md", "# hello\n"))
        cands = probe.candidates(diff)
        self.assertEqual(cands, [])

    def test_partial_edit_to_existing_function_is_skipped(self):
        # only a couple of '+' lines with no surrounding def -- can't be
        # cleanly reconstructed as a function on its own
        diff = "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -2,0 +3,1 @@\n+    return x + 2\n"
        cands = probe.candidates(diff)
        self.assertEqual(cands, [])

    def test_empty_diff_yields_no_candidates(self):
        self.assertEqual(probe.candidates(""), [])

    def test_method_added_to_existing_class_falls_back_to_bare_name(self):
        # the diff block for a newly-added method never includes the class
        # line -- documented limitation: qualname loses the class prefix
        method_block = (
            '    def parse(self, v):\n'
            '        """Parse. Returns None if invalid."""\n'
            '        return v or None\n'
        )
        diff = _diff(("mod.py", method_block))
        cands = probe.candidates(diff)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["qualname"], "parse")

    def test_duplicate_candidate_deduped(self):
        diff = _diff(("mod.py", DOCSTRING_FUNC), ("mod.py", DOCSTRING_FUNC))
        cands = probe.candidates(diff)
        self.assertEqual(len(cands), 1)


class TestBuildPromptCap(unittest.TestCase):
    """Fix 1: build_prompt must cap the inlined file source like every other
    prompt sink in this repo (CLAUDE.md convention), not inline it whole."""

    def test_large_source_capped_with_marker(self):
        candidate = {"file": "mod.py", "qualname": "f", "promise": "does a thing"}
        source = "x = 1\n" * 5000  # far larger than PROBE_SOURCE_MAX_CHARS
        text = probe.build_prompt(candidate, source, "mod")
        self.assertIn(f"[{len(source)} chars total]", text)
        # bounded: the prompt must not scale with the full source length
        self.assertLess(len(text), probe.PROBE_SOURCE_MAX_CHARS + 1000)

    def test_small_source_not_truncated(self):
        candidate = {"file": "mod.py", "qualname": "f", "promise": "does a thing"}
        source = "def f(x):\n    return x\n"
        text = probe.build_prompt(candidate, source, "mod")
        self.assertNotIn("chars total]", text)
        self.assertIn(source, text)


class TestRunProbes(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name)
        (self.repo / "mod.py").write_text(
            "def parse_version(v):\n"
            '    """Parse a version string. Returns None if the input is invalid."""\n'
            "    if not v:\n"
            "        raise ValueError('empty version')\n"
            "    return v.split('.')\n"
        )
        self.candidate = {
            "file": "mod.py", "qualname": "parse_version",
            "promise": "Parse a version string. Returns None if the input is invalid.",
        }

    def tearDown(self):
        self.td.cleanup()

    def test_diverging_probe_produces_finding_with_repro(self):
        diverging_probe = (
            "PROBE:\n"
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "from mod import parse_version\n"
            "try:\n"
            "    result = parse_version('')\n"
            "    print(f'CONSISTENT: returned {result!r}')\n"
            "except ValueError as e:\n"
            "    print(f'DIVERGES: raised ValueError({e}) instead of returning None')\n"
        )
        finding = probe.run_probes(self.candidate, self.repo, ask=lambda _text: diverging_probe)
        self.assertIsNotNone(finding)
        self.assertEqual(finding["file"], "mod.py")
        self.assertIn("raised ValueError", finding["issue"])
        self.assertIn("docstring promises", finding["issue"])
        self.assertIn("parse_version", finding["repro"])

    def test_consistent_probe_produces_no_finding(self):
        consistent_probe = (
            "PROBE:\n"
            "from mod import parse_version\n"
            "result = parse_version('1.2.3')\n"
            "print(f'CONSISTENT: got {result!r}')\n"
        )
        finding = probe.run_probes(self.candidate, self.repo, ask=lambda _text: consistent_probe)
        self.assertIsNone(finding)

    def test_probe_that_raises_produces_no_finding(self):
        # a broken probe (typo'd import, unrelated crash) is NOT a
        # contradiction -- precision first, this must never become a finding
        broken_probe = (
            "PROBE:\n"
            "from mod import totally_not_a_real_function\n"
            "totally_not_a_real_function()\n"
        )
        finding = probe.run_probes(self.candidate, self.repo, ask=lambda _text: broken_probe)
        self.assertIsNone(finding)

    def test_diverging_probe_wrapped_in_markdown_fence_still_found(self):
        # observed live against the real model: despite the prompt saying
        # "no markdown fences", the reply often wraps each probe in
        # ```python ... ``` -- unstripped, that's a SyntaxError as a .py
        # file, silently turning a real catch into "no finding".
        fenced_probe = (
            "PROBE:\n"
            "```python\n"
            "from mod import parse_version\n"
            "try:\n"
            "    result = parse_version('')\n"
            "except ValueError as e:\n"
            "    print(f'DIVERGES: raised ValueError({e!r}) instead of returning None')\n"
            "else:\n"
            "    print(f'DIVERGES: returned {result!r} instead of None')\n"
            "```\n"
        )
        finding = probe.run_probes(self.candidate, self.repo, ask=lambda _text: fenced_probe)
        self.assertIsNotNone(finding)
        self.assertIn("raised ValueError", finding["issue"])
        self.assertNotIn("```", finding["repro"])

    def test_second_probe_used_when_first_is_consistent(self):
        two_probes = (
            "PROBE:\n"
            "from mod import parse_version\n"
            "print(f'CONSISTENT: {parse_version(\"1.0\")!r}')\n"
            "PROBE:\n"
            "from mod import parse_version\n"
            "try:\n"
            "    parse_version(None)\n"
            "    print('CONSISTENT: ok')\n"
            "except ValueError as e:\n"
            "    print(f'DIVERGES: raised {e}')\n"
        )
        finding = probe.run_probes(self.candidate, self.repo, ask=lambda _text: two_probes)
        self.assertIsNotNone(finding)
        self.assertIn("DIVERGES", "".join(finding["repro"]))  # repro is the actual probe 2 text
        self.assertIn("parse_version(None)", finding["repro"])

    def test_malformed_reply_no_marker_yields_no_finding(self):
        finding = probe.run_probes(self.candidate, self.repo,
                                   ask=lambda _text: "I refuse to answer in that format.")
        self.assertIsNone(finding)

    def test_missing_file_yields_no_finding_no_ask_call(self):
        calls = []
        def ask(text):
            calls.append(text)
            return "PROBE:\nprint('CONSISTENT: n/a')\n"
        finding = probe.run_probes(
            {"file": "nope.py", "qualname": "x", "promise": "p"}, self.repo, ask=ask)
        self.assertIsNone(finding)
        self.assertEqual(calls, [])

    def test_ask_exception_yields_no_finding(self):
        def ask(_text):
            raise RuntimeError("model unavailable")
        finding = probe.run_probes(self.candidate, self.repo, ask=ask)
        self.assertIsNone(finding)


class TestCriticMainWiring(unittest.TestCase):
    """Budget gate, opt-in resolution, and byte-identical-when-off, all
    wired through critic.main."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name)
        self.suggestions = self.cc / "suggestions.ndjsonl"
        self.heuristics = self.cc / "heuristics.md"
        self.calls_log = self.cc / "calls.log"
        for i in range(5):
            (self.cc / f"mod{i}.py").write_text("x = 1\n")
        self.events = []
        # batch_diff_text (what probe_pass and judge_batch both read) only
        # looks at "commit"-type events plus ctx["latest_diff"] — same shape
        # heartbeat() populates ctx with, not a raw "diff" event in the list.
        self.base_ctx = {"heuristics_path": self.heuristics, "suggestions_file": self.suggestions,
                         "persona": "", "project": "", "repo": self.cc,
                         "beat": 1, "ts": "2026-01-01T00:00:00", "verify": False,
                         "latest_diff": {"payload": {"diff": self._five_candidate_diff()}}}

    def tearDown(self):
        os.environ.pop("CRITIC_CMD", None)
        self.td.cleanup()

    def _five_candidate_diff(self):
        blocks = []
        for i in range(5):
            blocks += [f"diff --git a/mod{i}.py b/mod{i}.py", "--- /dev/null",
                       f"+++ b/mod{i}.py", "@@ -0,0 +1,5 @@",
                       f"+def f{i}(x):",
                       f'+    """Doc for f{i}."""',
                       "+    return x"]
        return "\n".join(blocks)

    def _set_stub(self, script: str):
        stub = self.cc / "stub.py"
        stub.write_text(script)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        os.environ["CRITIC_CMD"] = str(stub)

    def _rows(self):
        if not self.suggestions.exists():
            return []
        return [json.loads(line) for line in self.suggestions.read_text().splitlines() if line]

    def _calls(self):
        if not self.calls_log.exists():
            return []
        return [ln for ln in self.calls_log.read_text().splitlines()]

    JUDGE_PASS_ALWAYS = """#!/usr/bin/env python3
import sys
from pathlib import Path
prompt_file = Path(sys.argv[1])
text = prompt_file.read_text()
calls_log = Path(__file__).parent / "calls.log"
if text.startswith("TASK: PROBE"):
    with calls_log.open("a") as f:
        f.write("probe\\n")
    print("PROBE:\\nprint('CONSISTENT: nothing to see')\\n")
else:
    print("PASS")
"""

    def test_budget_caps_five_candidates_to_two_model_calls(self):
        from critic.main import judge_batch
        self._set_stub(self.JUDGE_PASS_ALWAYS)
        ctx = {**self.base_ctx, "probes": True}
        judge_batch(self.events, ctx)
        self.assertEqual(len(self._calls()), probe.MAX_PROBE_CALLS_PER_BEAT)

    def test_probes_off_by_default_no_probe_calls_record_unaffected(self):
        from critic.main import judge_batch
        self._set_stub(self.JUDGE_PASS_ALWAYS)
        ctx = dict(self.base_ctx)  # no "probes" key at all
        judge_batch(self.events, ctx)
        self.assertEqual(self._calls(), [])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("source", rows[0])

    def test_probes_explicitly_false_no_probe_calls(self):
        from critic.main import judge_batch
        self._set_stub(self.JUDGE_PASS_ALWAYS)
        ctx = {**self.base_ctx, "probes": False}
        judge_batch(self.events, ctx)
        self.assertEqual(self._calls(), [])
        self.assertEqual(len(self._rows()), 1)

    def test_zero_candidates_zero_model_calls_even_with_probes_on(self):
        from critic.main import judge_batch
        self._set_stub(self.JUDGE_PASS_ALWAYS)
        ctx = {**self.base_ctx, "probes": True,
              "latest_diff": {"payload": {"diff": "+x = 1\n"}}}
        judge_batch([], ctx)
        self.assertEqual(self._calls(), [])

    def test_diverging_probe_becomes_its_own_suggestion_row(self):
        from critic.main import judge_batch
        script = """#!/usr/bin/env python3
import sys
from pathlib import Path
prompt_file = Path(sys.argv[1])
text = prompt_file.read_text()
if text.startswith("TASK: PROBE"):
    print(
        "PROBE:\\n"
        "try:\\n"
        "    1/0\\n"
        "except ZeroDivisionError as e:\\n"
        "    print(f'DIVERGES: {e}')\\n"
    )
else:
    print("PASS")
"""
        self._set_stub(script)
        # Single-candidate diff so the finding lands deterministically.
        (self.cc / "mod0.py").write_text(DOCSTRING_FUNC)
        ctx = {**self.base_ctx, "probes": True,
              "latest_diff": {"payload": {"diff": _diff(("mod0.py", DOCSTRING_FUNC))}}}
        judge_batch([], ctx)
        rows = self._rows()
        probe_rows = [r for r in rows if r.get("verdict") == "SUGGESTION"
                     and r.get("source") == "probe"]
        self.assertEqual(len(probe_rows), 1)
        s = probe_rows[0]["suggestion"]
        self.assertEqual(s["file"], "mod0.py")
        self.assertIn("docstring promises", s["issue"])

    def test_probe_pass_dedups_across_beats_via_probed_keys_state(self):
        """Fix 2 (the important one): without cross-beat memory, an
        uncommitted candidate stays in the diff every beat, so probe_pass
        would re-run TASK: PROBE and write a fresh-uuid SUGGESTION row each
        time. ctx["probed_keys"]/ctx["on_probed"] is the same mechanism
        critic.main's real daemon loop threads through critic-state.json
        (main()'s _on_probed callback + heartbeat()'s per-beat snapshot) --
        simulated here by threading a plain dict across two judge_batch
        calls, same shape."""
        from critic.main import judge_batch
        script = """#!/usr/bin/env python3
import sys
from pathlib import Path
prompt_file = Path(sys.argv[1])
text = prompt_file.read_text()
calls_log = Path(__file__).parent / "calls.log"
if text.startswith("TASK: PROBE"):
    with calls_log.open("a") as f:
        f.write("probe\\n")
    print(
        "PROBE:\\n"
        "try:\\n"
        "    1/0\\n"
        "except ZeroDivisionError as e:\\n"
        "    print(f'DIVERGES: {e}')\\n"
    )
else:
    print("PASS")
"""
        self._set_stub(script)
        (self.cc / "mod0.py").write_text(DOCSTRING_FUNC)
        diff = _diff(("mod0.py", DOCSTRING_FUNC))

        state: dict = {}

        def on_probed(keys):
            existing = state.get("probed_keys", [])
            state["probed_keys"] = existing + [k for k in keys if k not in existing]

        def make_ctx():
            return {**self.base_ctx, "probes": True,
                   "latest_diff": {"payload": {"diff": diff}},
                   "probed_keys": state.get("probed_keys", []),
                   "on_probed": on_probed}

        # Beat 1: nothing probed yet -> the model turn runs and the
        # divergence becomes a SUGGESTION row.
        judge_batch(self.events, make_ctx())
        self.assertEqual(len(self._calls()), 1)

        # Beat 2: same uncommitted candidate, still in the diff. The KEY
        # assertion -- probe_pass must recognize it as already-probed and
        # skip the model turn entirely, not spend a second call or deliver
        # a second finding.
        judge_batch(self.events, make_ctx())
        self.assertEqual(len(self._calls()), 1,
                         "probe_pass re-ran TASK: PROBE for an already-probed "
                         "candidate on the next beat")

        probe_rows = [r for r in self._rows() if r.get("verdict") == "SUGGESTION"
                     and r.get("source") == "probe"]
        self.assertEqual(len(probe_rows), 1,
                         "an already-probed candidate was re-delivered as a "
                         "second, fresh-uuid suggestion row")

    def test_transient_probe_error_does_not_suppress_candidate_next_beat(self):
        """Council catch: the dedup key must be recorded only when a probe runs
        to a conclusion. If run_probes RAISES (a transient model/staging
        hiccup), the candidate must be retried next beat, not marked probed and
        suppressed forever — otherwise a one-off error silently loses a real
        divergence."""
        from unittest import mock

        from critic import probe as probe_mod
        from critic.main import judge_batch
        self._set_stub("#!/usr/bin/env python3\nprint('PASS')\n")
        (self.cc / "mod0.py").write_text(DOCSTRING_FUNC)
        diff = _diff(("mod0.py", DOCSTRING_FUNC))

        state: dict = {}

        def on_probed(keys):
            existing = state.get("probed_keys", [])
            state["probed_keys"] = existing + [k for k in keys if k not in existing]

        def make_ctx():
            return {**self.base_ctx, "probes": True,
                   "latest_diff": {"payload": {"diff": diff}},
                   "probed_keys": state.get("probed_keys", []),
                   "on_probed": on_probed}

        with mock.patch.object(probe_mod, "run_probes",
                               side_effect=RuntimeError("transient")) as rp:
            judge_batch(self.events, make_ctx())
            self.assertEqual(rp.call_count, 1)
            self.assertEqual(state.get("probed_keys", []), [],
                             "a raised probe recorded the key, suppressing retry")
            # Beat 2: the same candidate must be attempted again, not skipped.
            judge_batch(self.events, make_ctx())
            self.assertEqual(rp.call_count, 2,
                             "candidate was suppressed after a transient error")


class TestRunScriptEnvScrub(unittest.TestCase):
    """Fix 1/2 (security hardening): run_script must never hand the model's
    script the parent's real environment (API keys, cloud creds) or its
    real HOME (~/.codecouncil/env, ~/.ssh) -- and must invoke the same
    interpreter running this test (sys.executable), not a hardcoded
    "python3" that may not exist / may be a different interpreter."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.staging = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_env_scrubbed_and_home_redirected_into_staging(self):
        os.environ["SECRET_XYZ"] = "top-secret-value-should-not-leak"
        try:
            script = (
                "import os\n"
                "print('SAW:', os.environ.get('SECRET_XYZ', 'none'))\n"
                "print('HOME:', os.environ.get('HOME'))\n"
            )
            res = probe.run_script(self.staging, script, timeout=10)
        finally:
            del os.environ["SECRET_XYZ"]
        self.assertIn("SAW: none", res.stdout)
        self.assertIn(f"HOME: {self.staging}", res.stdout)

    def test_uses_sys_executable(self):
        script = "import sys\nprint('EXE:', sys.executable)\n"
        res = probe.run_script(self.staging, script, timeout=10)
        self.assertIn(f"EXE: {sys.executable}", res.stdout)


class TestResolveProbes(unittest.TestCase):
    def test_flag_true_enables(self):
        from critic.main import resolve_probes
        self.assertTrue(resolve_probes(True, {}))

    def test_env_enables_when_no_flag(self):
        from critic.main import resolve_probes
        self.assertTrue(resolve_probes(False, {"COUNCIL_PROBES": "1"}))
        self.assertTrue(resolve_probes(False, {"COUNCIL_PROBES": "true"}))

    def test_default_off(self):
        from critic.main import resolve_probes
        self.assertFalse(resolve_probes(False, {}))


if __name__ == "__main__":
    unittest.main()
