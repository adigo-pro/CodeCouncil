"""gitwatch tests: untracked-content capture, caps, exclusions, fingerprint."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observer import gitwatch


class TestUntrackedContents(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)

    def tearDown(self):
        self.td.cleanup()

    def test_new_file_contents_captured(self):
        (self.repo / "new.py").write_text("def f():\n    return 1\n")
        snap = gitwatch.capture(self.repo)
        self.assertIn("new.py", snap["untracked_contents"])
        self.assertIn("return 1", snap["untracked_contents"]["new.py"])

    def test_binary_and_excluded_dirs_skipped(self):
        (self.repo / "img.bin").write_bytes(b"\x00\x01\x02")
        (self.repo / ".codecouncil").mkdir()
        (self.repo / ".codecouncil" / "state.json").write_text("{}")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "settings.json").write_text("{}")
        snap = gitwatch.capture(self.repo)
        self.assertEqual(snap["untracked_contents"], {})

    def test_per_file_cap(self):
        (self.repo / "big.txt").write_text("x" * 10_000)
        snap = gitwatch.capture(self.repo)
        text = snap["untracked_contents"]["big.txt"]
        self.assertLess(len(text), 4_100)
        self.assertIn("truncated", text)

    def test_editing_untracked_file_changes_fingerprint(self):
        (self.repo / "new.py").write_text("a = 1\n")
        fp1 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        (self.repo / "new.py").write_text("a = 2\n")
        fp2 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        self.assertNotEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
