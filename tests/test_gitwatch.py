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
        # CI runners have no global git identity; commits need a local one
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "t"], check=True)

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
        secret = "AKIAIOSFODNN7EXAMPLE"
        (self.repo / "creds.py").write_text(f"aws_key = '{secret}'\n")
        snap = gitwatch.capture(self.repo)
        text = snap["untracked_contents"]["creds.py"]
        self.assertNotIn(secret, text)
        self.assertIn("«REDACTED:aws-key»", text)

    def test_tracked_diff_secret_redacted(self):
        (self.repo / "config.py").write_text("aws_key = ''\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        secret = "AKIAIOSFODNN7EXAMPLE"
        (self.repo / "config.py").write_text(f"aws_key = '{secret}'\n")
        snap = gitwatch.capture(self.repo)
        self.assertNotIn(secret, snap["diff"])
        self.assertIn("«REDACTED:aws-key»", snap["diff"])

    def test_commit_diff_secret_redacted(self):
        (self.repo / "config.py").write_text("aws_key = ''\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        old = gitwatch.head(self.repo)
        secret = "AKIAIOSFODNN7EXAMPLE"
        (self.repo / "config.py").write_text(f"aws_key = '{secret}'\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "add key"], check=True)
        new = gitwatch.head(self.repo)
        c = gitwatch.capture_commits(self.repo, old, new)
        self.assertNotIn(secret, c["diff"])
        self.assertIn("«REDACTED:aws-key»", c["diff"])

    def test_fingerprint_stable_with_secret_across_calls(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        (self.repo / "creds.py").write_text(f"aws_key = '{secret}'\n")
        fp1 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        fp2 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        self.assertEqual(fp1, fp2)


class TestTouchedContents(unittest.TestCase):
    """Task 11: the critic sees the full current file, not just the -U8 hunk —
    the top false-positive source is flagging something the rest of the
    file already handles."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        # CI runners have no global git identity; commits need a local one
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "t"], check=True)

    def tearDown(self):
        self.td.cleanup()

    def _commit(self, path: str, text: str, msg: str = "init"):
        (self.repo / path).write_text(text)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", msg], check=True)

    def test_touched_paths_parsed_from_diff(self):
        self._commit("a.py", "x = 1\n")
        (self.repo / "a.py").write_text("x = 1\ny = 2\n")
        snap = gitwatch.capture(self.repo)
        self.assertIn("a.py", snap["touched_contents"])
        self.assertIn("y = 2", snap["touched_contents"]["a.py"])

    def test_touched_contents_redacted(self):
        self._commit("config.py", "aws_key = ''\n")
        secret = "AKIAIOSFODNN7EXAMPLE"
        (self.repo / "config.py").write_text(f"aws_key = '{secret}'\n")
        snap = gitwatch.capture(self.repo)
        text = snap["touched_contents"]["config.py"]
        self.assertNotIn(secret, text)
        self.assertIn("«REDACTED:aws-key»", text)

    def test_touched_per_file_cap(self):
        self._commit("big.txt", "x\n")
        (self.repo / "big.txt").write_text("x" * 10_000)
        snap = gitwatch.capture(self.repo)
        text = snap["touched_contents"]["big.txt"]
        self.assertLess(len(text), 6_100)
        self.assertIn("truncated", text)

    def test_touched_excludes_codecouncil_prefix(self):
        (self.repo / ".codecouncil").mkdir()
        self._commit(".codecouncil/notes.txt", "a\n")
        (self.repo / ".codecouncil" / "notes.txt").write_text("b\n")
        snap = gitwatch.capture(self.repo)
        self.assertEqual(snap["touched_contents"], {})

    def test_touched_skips_binary(self):
        self._commit("img.bin", "placeholder\n")
        (self.repo / "img.bin").write_bytes(b"\x00\x01\x02")
        snap = gitwatch.capture(self.repo)
        self.assertNotIn("img.bin", snap["touched_contents"])

    def test_touched_excludes_deleted_files(self):
        self._commit("gone.py", "x = 1\n")
        (self.repo / "gone.py").unlink()
        snap = gitwatch.capture(self.repo)
        self.assertNotIn("gone.py", snap["touched_contents"])

    def test_touched_excludes_untracked(self):
        # New (untracked) files never appear in `git diff` output — diff
        # compares against HEAD and an untracked file has no HEAD blob — so
        # this exercises the belt-and-suspenders exclude path directly.
        self._commit("a.py", "x = 1\n")
        touched = gitwatch._read_touched(self.repo, ["new.py", "a.py"], exclude={"new.py"})
        self.assertNotIn("new.py", touched)

    def test_fingerprint_stable_across_identical_captures(self):
        self._commit("a.py", "x = 1\n")
        (self.repo / "a.py").write_text("x = 1\ny = 2\n")
        fp1 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        fp2 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        self.assertEqual(fp1, fp2)

    def test_fingerprint_changes_when_touched_file_edited_again(self):
        self._commit("a.py", "x = 1\n")
        (self.repo / "a.py").write_text("x = 1\ny = 2\n")
        fp1 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        (self.repo / "a.py").write_text("x = 1\ny = 3\n")
        fp2 = gitwatch.fingerprint(gitwatch.capture(self.repo))
        self.assertNotEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
