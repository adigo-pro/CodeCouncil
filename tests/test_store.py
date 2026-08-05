"""core.store: NDJSON row plumbing + the atomic JSON writer."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import read_rows, write_json_atomic, write_text_atomic


class TestWriteJsonAtomic(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "nested" / "state.json"

    def test_unserializable_obj_leaves_no_orphan_tmp(self):
        # json.dump raises partway; the tmp file must be cleaned up, not left
        # littering the directory next to the state it failed to write.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(TypeError):
            write_json_atomic(self.path, {"x": object()})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(self.path.exists())

    def test_writes_valid_json_and_round_trips(self):
        write_json_atomic(self.path, {"beat": 3, "offsets": {"a": 1}})
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"beat": 3, "offsets": {"a": 1}},
        )

    def test_creates_parent_dirs(self):
        self.assertFalse(self.path.parent.exists())
        write_json_atomic(self.path, {"x": 1})
        self.assertTrue(self.path.exists())

    def test_interrupted_write_leaves_original_file_intact(self):
        # Simulate a crash mid-write: os.replace never happens, so the
        # pre-existing file must retain its old content untouched (the tmp
        # file is discarded, not the target).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"beat": 1}', encoding="utf-8")

        with mock.patch("core.store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_json_atomic(self.path, {"beat": 999})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"beat": 1}
        )


class TestReadRowsTolerance(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "rows.ndjsonl"

    def test_torn_multibyte_trailing_line_does_not_crash(self):
        # A row torn mid-multibyte-character (the file is appended mid-write,
        # and rows carry «REDACTED» / … markers) must skip the line, not raise
        # UnicodeDecodeError — read_rows feeds the reflector's unguarded reads.
        good = json.dumps({"a": "café«REDACTED:x»"}, ensure_ascii=False)
        data = good.encode("utf-8") + b"\n" + '{"b": "é'.encode("utf-8")[:-1]
        self.path.write_bytes(data)
        rows = read_rows(self.path)
        self.assertEqual(rows, [{"a": "café«REDACTED:x»"}])

    def test_skips_unparseable_complete_line(self):
        self.path.write_text('{"a":1}\ngarbage\n{"b":2}\n', encoding="utf-8")
        self.assertEqual(read_rows(self.path), [{"a": 1}, {"b": 2}])


class TestWriteTextAtomic(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "hist" / "v1.md"

    def test_round_trips_and_creates_parent(self):
        write_text_atomic(self.path, "version: 1\n- rule\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "version: 1\n- rule\n")

    def test_no_orphan_tmp_on_write_failure(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch("core.store.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                write_text_atomic(self.path, "x")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
