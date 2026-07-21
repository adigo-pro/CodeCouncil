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

    def test_commit_capture(self):
        (self.repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "first"], check=True)
        old = gitwatch.head(self.repo)
        (self.repo / "a.py").write_text("x = 2\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "bump x"], check=True)
        new = gitwatch.head(self.repo)
        self.assertNotEqual(old, new)
        c = gitwatch.capture_commits(self.repo, old, new)
        self.assertEqual(len(c["subjects"]), 1)
        self.assertIn("bump x", c["subjects"][0])
        self.assertIn("+x = 2", c["diff"])

    def test_commit_subject_secret_redacted(self):
        secret = "nvapi-" + "a" * 24
        (self.repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "first"], check=True)
        old = gitwatch.head(self.repo)
        (self.repo / "a.py").write_text("x = 2\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", f"oops committed {secret}"],
                       check=True)
        new = gitwatch.head(self.repo)
        c = gitwatch.capture_commits(self.repo, old, new)
        self.assertEqual(len(c["subjects"]), 1)
        self.assertNotIn(secret, c["subjects"][0])
        self.assertIn("«REDACTED:nvidia-key»", c["subjects"][0])

    def test_head_none_before_first_commit(self):
        self.assertIsNone(gitwatch.head(self.repo))

    def test_editing_untracked_file_changes_fingerprint(self):
        (self.repo / "new.py").write_text("a = 1\n")
        fp1 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        (self.repo / "new.py").write_text("a = 2\n")
        fp2 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        self.assertNotEqual(fp1, fp2)

    def test_untracked_secret_redacted(self):
        secret = "AKIAABCDEFGHIJKLMNOP"
        (self.repo / "creds.py").write_text(f"aws_key = '{secret}'\n")
        snap = gitwatch.capture(self.repo)
        text = snap["untracked_contents"]["creds.py"]
        self.assertNotIn(secret, text)
        self.assertIn("«REDACTED:aws-key»", text)

    def test_tracked_diff_secret_redacted(self):
        (self.repo / "config.py").write_text("aws_key = ''\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        secret = "AKIAABCDEFGHIJKLMNOP"
        (self.repo / "config.py").write_text(f"aws_key = '{secret}'\n")
        snap = gitwatch.capture(self.repo)
        self.assertNotIn(secret, snap["diff"])
        self.assertIn("«REDACTED:aws-key»", snap["diff"])

    def test_commit_diff_secret_redacted(self):
        (self.repo / "config.py").write_text("aws_key = ''\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        old = gitwatch.head(self.repo)
        secret = "AKIAABCDEFGHIJKLMNOP"
        (self.repo / "config.py").write_text(f"aws_key = '{secret}'\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "add key"], check=True)
        new = gitwatch.head(self.repo)
        c = gitwatch.capture_commits(self.repo, old, new)
        self.assertNotIn(secret, c["diff"])
        self.assertIn("«REDACTED:aws-key»", c["diff"])

    def test_fingerprint_stable_with_secret_across_calls(self):
        secret = "AKIAABCDEFGHIJKLMNOP"
        (self.repo / "creds.py").write_text(f"aws_key = '{secret}'\n")
        fp1 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        fp2 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        self.assertEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
