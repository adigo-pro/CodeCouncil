"""Acceptance-per-heuristics-version report — the improvement curve as a table.

    python3 -m reflector.report /path/to/watched/repo
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from .main import read_ndjson

GRADED = ("accepted", "rebutted", "ignored")


def build_rows(suggestions: list[dict], outcomes: list[dict]) -> list[dict]:
    per: dict[int, dict] = defaultdict(lambda: {
        "suggested": 0, "delivered": 0,
        "accepted": 0, "rebutted": 0, "ignored": 0, "undelivered": 0,
    })
    for s in suggestions:
        if s.get("verdict") == "SUGGESTION":
            per[s.get("heuristics_version", 0)]["suggested"] += 1
    for o in outcomes:
        v = per[o.get("heuristics_version", 0)]
        outcome = o.get("outcome")
        if outcome in GRADED:
            v[outcome] += 1
            v["delivered"] += 1
        elif outcome == "undelivered":
            v["undelivered"] += 1
    rows = []
    for version in sorted(per):
        v = per[version]
        graded = v["accepted"] + v["rebutted"] + v["ignored"]
        rows.append({
            "version": version, **v,
            "acceptance": (v["accepted"] / graded) if graded else None,
        })
    return rows


def render(rows: list[dict]) -> str:
    header = f"{'version':>7}  {'suggested':>9}  {'delivered':>9}  {'accepted':>8}  {'rebutted':>8}  {'ignored':>7}  {'undeliv':>7}  {'acceptance':>10}"
    lines = [header, "-" * len(header)]
    for r in rows:
        acc = f"{r['acceptance']:.0%}" if r["acceptance"] is not None else "—"
        lines.append(
            f"{'v' + str(r['version']):>7}  {r['suggested']:>9}  {r['delivered']:>9}  "
            f"{r['accepted']:>8}  {r['rebutted']:>8}  {r['ignored']:>7}  "
            f"{r['undelivered']:>7}  {acc:>10}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reflector.report", description=__doc__)
    ap.add_argument("repo", type=Path)
    args = ap.parse_args(argv)
    cc = args.repo.resolve() / ".codecouncil"
    rows = build_rows(read_ndjson(cc / "suggestions.ndjsonl"), read_ndjson(cc / "outcomes.ndjsonl"))
    if not rows:
        print("no data yet")
        return 0
    print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
