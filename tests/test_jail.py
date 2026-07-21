"""Tests for critic/pi_extensions/jail.mjs's jailPath — the path-containment
check that stands between a judgment turn's read-only repo tools
(critic/pi_extensions/repo_tools.mjs) and the developer's home directory
(Task 4 hardening: pi's *builtin* read/grep/find/ls resolve absolute/~ paths
straight through regardless of cwd, so the critic's judgment turn cannot
safely use them with cwd=<watched repo>; these jailed tools replace them).

jailPath lives in jail.mjs rather than repo_tools.mjs itself specifically so
it stays importable via plain `node` with zero npm dependencies —
repo_tools.mjs needs a static top-level `@sinclair/typebox` import for its
tool parameter schemas (a dynamic import deferred into the extension
factory was tried first and measured unreliable in a real pi turn), and a
top-level typebox import in the same file would make bare-node import of
jailPath fail before any export became reachable.

Requires `node`. Skipped when `node` isn't on PATH; these are the only
jail-logic tests in the suite, so a machine without node gets no coverage of
the jail itself — acceptable per the brief, since CI's python job has no
node (the `ui/` CI job does, but doesn't run these). Run locally (or in the
ui job's environment) to exercise them for real.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JAIL_MODULE = Path(__file__).resolve().parents[1] / "critic" / "pi_extensions" / "jail.mjs"


@unittest.skipUnless(shutil.which("node"), "node required for jailPath tests")
class TestJailPath(unittest.TestCase):
    def _run_cases(self, root: Path, cases: list[str]) -> dict:
        # "input" (not "path") holds the original case string — jailPath's
        # own result also has a "path" key on success (the resolved,
        # absolute path), which would otherwise clobber it via spread.
        js = f"""
import {{ jailPath }} from {json.dumps(str(JAIL_MODULE))};
const root = {json.dumps(str(root))};
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map((p) => ({{ input: p, ...jailPath(root, p) }}))));
"""
        res = subprocess.run(
            ["node", "--input-type=module", "-e", js],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return {r["input"]: r for r in json.loads(res.stdout)}

    def test_in_repo_path_resolves_under_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            (root / "subdir").mkdir(parents=True)
            (root / "subdir" / "file.txt").write_text("hi")

            results = self._run_cases(root, ["subdir/file.txt"])

            self.assertTrue(results["subdir/file.txt"]["ok"])
            resolved = Path(results["subdir/file.txt"]["path"])
            self.assertTrue(
                str(resolved).startswith(str(root.resolve()) + os.sep)
            )

    def test_symlink_escaping_root_rejected(self):
        # The exact vector the original Critical centered on: a symlink
        # *inside* the jail whose target lives outside it. realpathSync
        # follows the link, so the containment check must catch the
        # resolved (post-symlink) path, not just the literal request path.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "root"
            root.mkdir()
            real_target = base / "real_target"
            real_target.write_text("outside the jail, reached via symlink\n")
            os.symlink(real_target, root / "link_out")

            results = self._run_cases(root, ["link_out"])

            self.assertFalse(results["link_out"]["ok"])
            self.assertIn("outside", results["link_out"]["reason"])

    def test_symlink_within_repo_resolves(self):
        # An in-repo relative symlink (pointing back into the same
        # directory via ../subdir/) must still resolve and be accepted —
        # the jail rejects escapes, not symlinks per se.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            (root / "subdir").mkdir(parents=True)
            (root / "subdir" / "real.txt").write_text("in-repo target\n")
            os.symlink(Path("../subdir/real.txt"), root / "subdir" / "link_in")

            results = self._run_cases(root, ["subdir/link_in"])

            self.assertTrue(results["subdir/link_in"]["ok"], results["subdir/link_in"])
            resolved = Path(results["subdir/link_in"]["path"])
            self.assertTrue(str(resolved).endswith(str(Path("subdir") / "real.txt")))

    def test_directory_symlink_escaping_root_rejected(self):
        # A symlinked *directory* inside the jail pointing outside it —
        # walking through it (e.g. "dirlink/anything") must be rejected too,
        # not just a direct symlink-to-file escape.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "root"
            root.mkdir()
            outside_dir = base / "outside_dir"
            outside_dir.mkdir()
            (outside_dir / "anything").write_text("outside, via a dir symlink\n")
            os.symlink(outside_dir, root / "dirlink", target_is_directory=True)

            results = self._run_cases(root, ["dirlink/anything"])

            self.assertFalse(results["dirlink/anything"]["ok"])

    def test_escapes_and_workspace_paths_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "root"
            root.mkdir()
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("[core]\n")
            (root / ".codecouncil").mkdir()
            (root / ".codecouncil" / "suggestions.ndjsonl").write_text("{}\n")
            # A real target that exists just outside the jail, so the
            # containment check is proven against a path that would
            # otherwise resolve successfully — not just an ENOENT.
            (base / "escape").write_text("outside the jail\n")

            cases = [
                "../escape",
                "/etc/passwd",
                "~/x",
                ".codecouncil/suggestions.ndjsonl",
                ".git/config",
            ]
            results = self._run_cases(root, cases)

            for case in cases:
                self.assertFalse(
                    results[case]["ok"], f"expected jailPath to reject {case!r}, got {results[case]!r}"
                )


if __name__ == "__main__":
    unittest.main()
