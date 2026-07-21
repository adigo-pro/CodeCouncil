"""Shared fixture: no test may write into the git-tracked
evals/cases-harvested/ directory by omission.

reflector.harvest.maybe_harvest() and evals.run.load_cases() both read a
module-level path constant (harvest.HARVESTED_DIR, evals.run.HARVESTED_CASES_DIR)
that — unpatched — points at the real evals/cases-harvested/ inside this repo.
Any test that exercises reflector.main.grade_pending() (which calls
maybe_harvest for every graded row, unconditionally) risks writing a stray
harvest-<id>.json into that tracked directory if it forgets to patch both
constants. Inheriting HarvestIsolatedTestCase makes that impossible: setUp
redirects both constants to a per-test tempdir before any test method runs.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evals.run as evals_run  # noqa: E402
from reflector import harvest  # noqa: E402


class HarvestIsolatedTestCase(unittest.TestCase):
    """Base TestCase that isolates reflector.harvest / evals.run from the
    real evals/cases-harvested/ directory. Subclasses get self.harvested_dir
    (a per-test tempdir, not yet created on disk — harvest.maybe_harvest
    creates it lazily via mkdir(parents=True)) pointed to by both patched
    constants."""

    def setUp(self) -> None:
        super().setUp()
        self._harvest_isolation_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._harvest_isolation_tmp.cleanup)
        self.harvested_dir = Path(self._harvest_isolation_tmp.name) / "cases-harvested"

        harvest_patcher = mock.patch.object(harvest, "HARVESTED_DIR", self.harvested_dir)
        evals_patcher = mock.patch.object(evals_run, "HARVESTED_CASES_DIR", self.harvested_dir)
        harvest_patcher.start()
        evals_patcher.start()
        self.addCleanup(harvest_patcher.stop)
        self.addCleanup(evals_patcher.stop)
