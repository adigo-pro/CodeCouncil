"""core.store: NDJSON row plumbing + the atomic JSON writer."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import write_json_atomic


class TestWriteJsonAtomic(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "nested" / "state.json"

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


if __name__ == "__main__":
    unittest.main()
