"""reflector.harvest: auto-grow the frozen eval set from real graded outcomes."""

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run import load_cases
from observer import transcript
from reflector import harvest
from tests.harvest_isolation import HarvestIsolatedTestCase


def _suggestion_row(sid="s1", file="a.py", issue="off-by-one", verification=None,
                    file_touched=None, verdict="SUGGESTION"):
    row = {"id": sid, "verdict": verdict,
           "suggestion": {"file": file, "line": 3, "severity": "high",
                          "issue": issue, "rationale": "r"}}
    if verification is not None:
        row["verification"] = verification
    if file_touched is not None:
        row["file_touched"] = file_touched
    return row


def _write_material(cc: Path, sid: str, events=None, latest_diff=None) -> None:
    d = cc / "case-material"
    d.mkdir(parents=True, exist_ok=True)
    material = {"events": events if events is not None else [{"type": "diff"}],
               "latest_diff": latest_diff}
    (d / f"{sid}.json").write_text(json.dumps(material), encoding="utf-8")


class TestMaybeHarvest(HarvestIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.td = tempfile.TemporaryDirectory()
        self.cc = Path(self.td.name) / ".codecouncil"

    def tearDown(self):
        self.td.cleanup()

    def test_accepted_verified_writes_flag_case(self):
        _write_material(self.cc, "s1", events=[{"type": "diff", "payload": {"diff": "+x"}}],
                        latest_diff={"type": "diff", "payload": {"diff": "+x"}})
        row = _suggestion_row("s1", file="a.py", issue="off-by-one",
                              verification={"status": "verified"})
        name = harvest.maybe_harvest(self.cc, row, "accepted")
        self.assertEqual(name, "harvest-s1")
        case_path = self.harvested_dir / "harvest-s1.json"
        self.assertTrue(case_path.exists())
        case = json.loads(case_path.read_text())
        self.assertEqual(case["expected"], "flag")
        self.assertEqual(case["expect_files"], ["a.py"])
        self.assertEqual(case["events"], [{"type": "diff", "payload": {"diff": "+x"}}])
        self.assertEqual(case["latest_diff"], {"type": "diff", "payload": {"diff": "+x"}})

    def test_accepted_no_verification_field_still_flag_case(self):
        _write_material(self.cc, "s1")
        row = _suggestion_row("s1")  # no "verification" key at all
        name = harvest.maybe_harvest(self.cc, row, "accepted")
        self.assertEqual(name, "harvest-s1")

    def test_accepted_verified_case_is_loadable_by_evals_run(self):
        # harvest.HARVESTED_DIR and evals.run.HARVESTED_CASES_DIR both name the
        # same real directory (evals/cases-harvested/) independently — in
        # production they always agree; HarvestIsolatedTestCase.setUp already
        # points both at the same temp dir (self.harvested_dir) to stay
        # hermetic against the real repo's contents.
        _write_material(self.cc, "s1", events=[{"type": "diff", "payload": {"diff": "+x"}}])
        row = _suggestion_row("s1", verification={"status": "verified"})
        harvest.maybe_harvest(self.cc, row, "accepted")
        with mock.patch("evals.run.CASES_DIR", Path(self.td.name) / "no-hand-made"):
            cases = load_cases()
        names = [c["name"] for c in cases]
        self.assertIn("harvest-s1", names)

    def test_rebutted_file_not_touched_writes_pass_case(self):
        _write_material(self.cc, "s2")
        row = _suggestion_row("s2", file="b.py", issue="wrong", file_touched=False)
        name = harvest.maybe_harvest(self.cc, row, "rebutted")
        self.assertEqual(name, "harvest-s2")
        case = json.loads((self.harvested_dir / "harvest-s2.json").read_text())
        self.assertEqual(case["expected"], "pass")
        self.assertEqual(case["expect_files"], [])

    def test_rebutted_file_touched_writes_no_case(self):
        _write_material(self.cc, "s3")
        row = _suggestion_row("s3", file_touched=True)
        name = harvest.maybe_harvest(self.cc, row, "rebutted")
        self.assertIsNone(name)
        self.assertFalse((self.harvested_dir / "harvest-s3.json").exists())

    def test_verification_refuted_writes_pass_case_regardless_of_outcome(self):
        _write_material(self.cc, "s4")
        row = _suggestion_row("s4", verification={"status": "refuted"})
        name = harvest.maybe_harvest(self.cc, row, "ignored")
        self.assertEqual(name, "harvest-s4")
        case = json.loads((self.harvested_dir / "harvest-s4.json").read_text())
        self.assertEqual(case["expected"], "pass")

    def test_ignored_with_no_verification_writes_nothing(self):
        _write_material(self.cc, "s5")
        row = _suggestion_row("s5")
        self.assertIsNone(harvest.maybe_harvest(self.cc, row, "ignored"))

    def test_undelivered_writes_nothing(self):
        _write_material(self.cc, "s6")
        row = _suggestion_row("s6")
        self.assertIsNone(harvest.maybe_harvest(self.cc, row, "undelivered"))

    def test_non_suggestion_verdict_writes_nothing(self):
        row = _suggestion_row("s7", verdict="PASS")
        self.assertIsNone(harvest.maybe_harvest(self.cc, row, "accepted"))

    def test_missing_case_material_returns_none(self):
        row = _suggestion_row("nope", verification={"status": "verified"})
        self.assertIsNone(harvest.maybe_harvest(self.cc, row, "accepted"))
        self.assertFalse(self.harvested_dir.exists() and
                         any(self.harvested_dir.glob("*.json")))

    def test_harvested_case_carries_redaction_not_the_secret(self):
        """Redaction happens upstream in observer.transcript.parse_line, before
        an event ever reaches case-material — a harvested case built from a
        real transcript line containing a secret must show the marker, never
        the raw value. This is what makes evals/cases-harvested/ (git-tracked)
        safe to version."""
        secret = "nvapi-" + "b" * 30
        line = json.dumps({
            "type": "assistant", "sessionId": "s",
            "message": {"content": [
                {"type": "thinking", "thinking": f"using key {secret} to call the API"},
            ]},
        })
        (event,) = transcript.parse_line(line, beat=1)
        events = [asdict(event)]
        self.assertNotIn(secret, json.dumps(events))  # sanity: already redacted going in
        _write_material(self.cc, "s1", events=events)

        row = _suggestion_row("s1", verification={"status": "verified"})
        name = harvest.maybe_harvest(self.cc, row, "accepted")
        self.assertEqual(name, "harvest-s1")

        case_text = (self.harvested_dir / "harvest-s1.json").read_text()
        self.assertNotIn(secret, case_text)
        case = json.loads(case_text)
        reasoning_text = case["events"][0]["payload"]["text"]
        self.assertIn("«REDACTED:nvidia-key»", reasoning_text)

    def test_dedupe_by_file_and_issue_content_hash(self):
        _write_material(self.cc, "s1", events=[{"type": "diff", "payload": {"diff": "+x"}}])
        _write_material(self.cc, "s1-again", events=[{"type": "diff", "payload": {"diff": "+y"}}])
        row1 = _suggestion_row("s1", file="a.py", issue="off-by-one",
                               verification={"status": "verified"})
        row2 = _suggestion_row("s1-again", file="a.py", issue="off-by-one",
                               verification={"status": "verified"})
        first = harvest.maybe_harvest(self.cc, row1, "accepted")
        second = harvest.maybe_harvest(self.cc, row2, "accepted")
        self.assertEqual(first, "harvest-s1")
        self.assertIsNone(second)
        self.assertEqual(len(list(self.harvested_dir.glob("*.json"))), 1)

    def test_different_issue_same_file_not_deduped(self):
        _write_material(self.cc, "s1")
        _write_material(self.cc, "s2")
        row1 = _suggestion_row("s1", file="a.py", issue="off-by-one",
                               verification={"status": "verified"})
        row2 = _suggestion_row("s2", file="a.py", issue="unbounded read",
                               verification={"status": "verified"})
        harvest.maybe_harvest(self.cc, row1, "accepted")
        second = harvest.maybe_harvest(self.cc, row2, "accepted")
        self.assertEqual(second, "harvest-s2")

    def test_missed_outcome_creates_must_flag_case_with_miss_file(self):
        _write_material(self.cc, "p1", events=[{"type": "diff", "payload": {"diff": "+x"}}])
        row = {"id": "p1", "verdict": "PASS", "reason": "looked fine at review time"}
        name = harvest.maybe_harvest(self.cc, row, "missed", miss_file="src/a.py")
        self.assertEqual(name, "harvest-p1")
        case = json.loads((self.harvested_dir / "harvest-p1.json").read_text())
        self.assertEqual(case["expected"], "flag")
        self.assertEqual(case["expect_files"], ["a.py"])

    def test_missed_without_miss_file_writes_nothing(self):
        _write_material(self.cc, "p2")
        row = {"id": "p2", "verdict": "PASS"}
        self.assertIsNone(harvest.maybe_harvest(self.cc, row, "missed"))

    def test_missed_missing_case_material_returns_none(self):
        row = {"id": "no-material", "verdict": "PASS"}
        self.assertIsNone(harvest.maybe_harvest(self.cc, row, "missed", miss_file="a.py"))
        self.assertFalse(self.harvested_dir.exists() and
                         any(self.harvested_dir.glob("*.json")))

    def test_cap_evicts_oldest_harvested_file(self):
        """Beyond MAX_HARVESTED, harvest.maybe_harvest prunes the oldest
        harvested case by mtime (mirrors critic/main.py's _prune_dir) rather
        than refusing to harvest once full — a 4th case (cap=3) evicts the
        1st, and evals/cases/ (hand-made, a separate directory entirely) is
        never touched by this."""
        import os

        old_max = harvest.MAX_HARVESTED
        harvest.MAX_HARVESTED = 3
        try:
            for i in range(3):
                sid = f"cap{i}"
                _write_material(self.cc, sid)
                row = _suggestion_row(sid, file=f"f{i}.py", issue=f"issue{i}",
                                      verification={"status": "verified"})
                name = harvest.maybe_harvest(self.cc, row, "accepted")
                self.assertEqual(name, f"harvest-{sid}")
                os.utime(self.harvested_dir / f"{name}.json", (i, i))
            _write_material(self.cc, "cap-overflow")
            row = _suggestion_row("cap-overflow", file="overflow.py", issue="overflow issue",
                                  verification={"status": "verified"})
            name = harvest.maybe_harvest(self.cc, row, "accepted")
            self.assertEqual(name, "harvest-cap-overflow")
            names = sorted(p.name for p in self.harvested_dir.glob("*.json"))
            self.assertEqual(len(names), 3)
            self.assertNotIn("harvest-cap0.json", names)  # oldest evicted
            self.assertIn("harvest-cap-overflow.json", names)
        finally:
            harvest.MAX_HARVESTED = old_max

    def test_missed_dedupe_includes_commit_subject_two_fixes_two_cases(self):
        """Two misses on the same file with different fix commits must
        harvest as two distinct cases — the old (file, "") dedupe hash
        collapsed them into one, losing the second fix's signal."""
        _write_material(self.cc, "p1")
        _write_material(self.cc, "p2")
        row1 = {"id": "p1", "verdict": "PASS", "reason": "looked fine"}
        row2 = {"id": "p2", "verdict": "PASS", "reason": "looked fine"}
        first = harvest.maybe_harvest(self.cc, row1, "missed", miss_file="a.py",
                                      commit_subject="fix null deref")
        second = harvest.maybe_harvest(self.cc, row2, "missed", miss_file="a.py",
                                       commit_subject="fix off-by-one")
        self.assertEqual(first, "harvest-p1")
        self.assertEqual(second, "harvest-p2")
        self.assertEqual(len(list(self.harvested_dir.glob("*.json"))), 2)

    def test_missed_dedupe_same_commit_subject_collapses(self):
        _write_material(self.cc, "p1")
        _write_material(self.cc, "p1-again")
        row1 = {"id": "p1", "verdict": "PASS", "reason": "looked fine"}
        row2 = {"id": "p1-again", "verdict": "PASS", "reason": "looked fine"}
        first = harvest.maybe_harvest(self.cc, row1, "missed", miss_file="a.py",
                                      commit_subject="fix null deref")
        second = harvest.maybe_harvest(self.cc, row2, "missed", miss_file="a.py",
                                       commit_subject="fix null deref")
        self.assertEqual(first, "harvest-p1")
        self.assertIsNone(second)
        self.assertEqual(len(list(self.harvested_dir.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
