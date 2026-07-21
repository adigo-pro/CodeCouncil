"""Tests for core.store: bounded and full NDJSON readers."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import store


class TestReadTailRows(unittest.TestCase):
    def _write(self, td: str, rows: list[dict]) -> Path:
        p = Path(td) / "obs.ndjsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def test_small_file_returns_all(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, [{"i": i} for i in range(5)])
            rows = store.read_tail_rows(p)
            self.assertEqual([r["i"] for r in rows], [0, 1, 2, 3, 4])

    def test_large_file_returns_only_tail_and_drops_partial_first_line(self):
        with tempfile.TemporaryDirectory() as td:
            # each row ~ padded to a known size; cap well under total
            rows = [{"i": i, "pad": "x" * 100} for i in range(200)]
            p = self._write(td, rows)
            got = store.read_tail_rows(p, max_bytes=2_000)
            self.assertGreater(len(got), 0)
            self.assertLess(len(got), 200)  # bounded
            # every returned row parsed cleanly (partial first line was dropped)
            self.assertTrue(all("i" in r and "pad" in r for r in got))
            # it's the *tail*: last row is present
            self.assertEqual(got[-1]["i"], 199)

    def test_missing_file(self):
        self.assertEqual(store.read_tail_rows(Path("/nonexistent/x.ndjsonl")), [])

    def test_skips_unparseable_lines(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "obs.ndjsonl"
            p.write_text('{"a":1}\nnot json\n{"b":2}\n', encoding="utf-8")
            rows = store.read_tail_rows(p)
            self.assertEqual(rows, [{"a": 1}, {"b": 2}])


if __name__ == "__main__":
    unittest.main()
