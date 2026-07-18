"""Observer tests against a real Claude Code session transcript (tests/fixtures/)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observer import transcript
from observer.events import DIFF, REASONING, TOOL_CALL, Event, EventLog
from observer.state import State

FIXTURE = Path(__file__).parent / "fixtures" / "session.jsonl"


class TestParseLine(unittest.TestCase):
    def test_fixture_yields_reasoning_and_tool_calls(self):
        events = []
        for line in FIXTURE.read_text(encoding="utf-8").splitlines():
            events.extend(transcript.parse_line(line, beat=1))
        kinds = {e.type for e in events}
        self.assertIn(REASONING, kinds)
        self.assertIn(TOOL_CALL, kinds)
        for e in events:
            self.assertIsNotNone(e.session)

    def test_non_assistant_lines_ignored(self):
        self.assertEqual(transcript.parse_line('{"type":"user"}', 1), [])
        self.assertEqual(transcript.parse_line("not json", 1), [])

    def test_sidechain_skipped(self):
        line = json.dumps(
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "hi"}]},
            }
        )
        self.assertEqual(transcript.parse_line(line, 1), [])

    def test_long_reasoning_truncated(self):
        line = json.dumps(
            {
                "type": "assistant",
                "sessionId": "s",
                "message": {"content": [{"type": "thinking", "thinking": "x" * 5000}]},
            }
        )
        (event,) = transcript.parse_line(line, 1)
        self.assertLess(len(event.payload["text"]), 1600)
        self.assertIn("5000 chars total", event.payload["text"])


class TestTailing(unittest.TestCase):
    def test_incremental_offsets_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.jsonl"
            f.write_bytes(b'{"a":1}\n{"b":2}\n{"partial"')
            lines, off = transcript.tail_new_lines(f, 0)
            self.assertEqual(lines, ['{"a":1}', '{"b":2}'])
            # partial trailing line must not be consumed
            self.assertEqual(off, len(b'{"a":1}\n{"b":2}\n'))

            f.write_bytes(b'{"a":1}\n{"b":2}\n{"partial":3}\n')
            lines, off = transcript.tail_new_lines(f, off)
            self.assertEqual(lines, ['{"partial":3}'])

            lines, off2 = transcript.tail_new_lines(f, off)
            self.assertEqual(lines, [])
            self.assertEqual(off2, off)

    def test_truncated_file_rereads(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "s.jsonl"
            f.write_bytes(b'{"a":1}\n')
            lines, _ = transcript.tail_new_lines(f, 999)
            self.assertEqual(lines, ['{"a":1}'])


class TestCollectFromFixtureDir(unittest.TestCase):
    def test_collect_full_then_nothing_new(self):
        offsets: dict[str, int] = {}
        events = transcript.collect(FIXTURE.parent, offsets, beat=1)
        self.assertGreater(len(events), 5)
        again = transcript.collect(FIXTURE.parent, offsets, beat=2)
        self.assertEqual(again, [])


class TestStateAndLog(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s = State(offsets={"a": 5}, last_diff_hash="h", beat=3)
            s.save(p)
            loaded = State.load(p)
            self.assertEqual((loaded.offsets, loaded.last_diff_hash, loaded.beat), ({"a": 5}, "h", 3))

    def test_event_log_is_valid_ndjson(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "obs.ndjsonl"
            EventLog(p).append([Event(beat=1, type=DIFF, payload={"diff": ""})])
            EventLog(p).append([Event(beat=2, type=REASONING, session="s", payload={"text": "t"})])
            rows = [json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual([r["beat"] for r in rows], [1, 2])
            self.assertEqual([r["type"] for r in rows], [DIFF, REASONING])


if __name__ == "__main__":
    unittest.main()
