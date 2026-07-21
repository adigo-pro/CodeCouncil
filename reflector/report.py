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


def consistent(outcome: dict) -> bool | None:
    """Model grade vs the deterministic file_touched signal. None = not applicable."""
    touched = outcome.get("file_touched")
    if touched is None or outcome.get("outcome") not in GRADED:
        return None
    if outcome["outcome"] == "accepted":
        return touched
    if outcome["outcome"] == "ignored":
        return not touched
    return True  # rebutted happens in words; either way is consistent


def build_rows(suggestions: list[dict], outcomes: list[dict]) -> list[dict]:
    per: dict[int, dict] = defaultdict(lambda: {
        "suggested": 0, "delivered": 0,
        "accepted": 0, "rebutted": 0, "ignored": 0, "undelivered": 0, "missed": 0,
        "xcheck_ok": 0, "xcheck_n": 0,
    })
    inconsistent = []
    for s in suggestions:
        if s.get("verdict") == "SUGGESTION":
            per[s.get("heuristics_version", 0)]["suggested"] += 1
    for o in outcomes:
        v = per[o.get("heuristics_version", 0)]
        outcome = o.get("outcome")
        if outcome in GRADED:
            v[outcome] += 1
            v["delivered"] += 1
            ok = consistent(o)
            if ok is not None:
                v["xcheck_n"] += 1
                v["xcheck_ok"] += int(ok)
                if not ok:
                    inconsistent.append(o)
        elif outcome == "undelivered":
            v["undelivered"] += 1
        elif outcome == "missed":
            # "missed" = a PASS verdict later contradicted by a fix commit
            # (reflector.misses). It is a distinct failure mode from a
            # delivered-and-graded suggestion (accepted/rebutted/ignored) and
            # deliberately stays OUT of the acceptance-rate denominator: the
            # two measurement signals (acceptance rate here, frozen evals in
            # evals/) must stay independent per CLAUDE.md's two-signal
            # separation rule — folding misses into acceptance would let one
            # failure mode silently move the other's gate/rollback math.
            v["missed"] += 1
    rows = []
    for version in sorted(per):
        v = per[version]
        graded = v["accepted"] + v["rebutted"] + v["ignored"]
        rows.append({
            "version": version, **v,
            "acceptance": (v["accepted"] / graded) if graded else None,
            "xcheck": (v["xcheck_ok"] / v["xcheck_n"]) if v["xcheck_n"] else None,
            "inconsistent": inconsistent,
        })
    return rows


def build_rule_rows(suggestions: list[dict], outcomes: list[dict]) -> list[dict]:
    """Per (heuristics_version, rule) suggested/accepted/rebutted/ignored
    counts — the surgical companion to build_rows: instead of "how did v3
    do overall", this answers "did R3 specifically do well", which is what
    a rewrite needs to decide "drop R3" vs "keep R3, drop R5".

    "suggested" comes from `suggestions` (any verdict=="SUGGESTION" row,
    whether or not it was ever delivered/graded — same convention as
    build_rows); accepted/rebutted/ignored come from `outcomes` for the
    matching (version, rule). A row with no "rule" (parse_reply defaults it
    to null when the model didn't cite one; also true of any suggestion made
    before this feature existed) lands in the rule=None bucket — rendered
    "R?" by render_rule_rows — so cite-less findings stay visible in the
    table instead of silently vanishing.

    Sorted by (version, rule), with the rule=None bucket sorted last within
    its version."""
    per: dict[tuple[int, int | None], dict] = defaultdict(lambda: {
        "suggested": 0, "accepted": 0, "rebutted": 0, "ignored": 0,
    })
    for s in suggestions:
        if s.get("verdict") == "SUGGESTION":
            key = (s.get("heuristics_version", 0), (s.get("suggestion") or {}).get("rule"))
            per[key]["suggested"] += 1
    for o in outcomes:
        outcome = o.get("outcome")
        if outcome in GRADED:
            key = (o.get("heuristics_version", 0), o.get("rule"))
            per[key][outcome] += 1
    rows = [{"version": v, "rule": r, **counts} for (v, r), counts in per.items()]
    rows.sort(key=lambda row: (row["version"], row["rule"] is None, row["rule"] or 0))
    return rows


def render_rule_rows(rows: list[dict]) -> str:
    header = f"{'version':>7}  {'rule':>5}  {'suggested':>9}  {'accepted':>8}  {'rebutted':>8}  {'ignored':>7}"
    lines = [header, "-" * len(header)]
    for r in rows:
        rule_label = f"R{r['rule']}" if r["rule"] is not None else "R?"
        lines.append(
            f"{'v' + str(r['version']):>7}  {rule_label:>5}  {r['suggested']:>9}  "
            f"{r['accepted']:>8}  {r['rebutted']:>8}  {r['ignored']:>7}"
        )
    return "\n".join(lines)


def render(rows: list[dict]) -> str:
    header = f"{'version':>7}  {'suggested':>9}  {'delivered':>9}  {'accepted':>8}  {'rebutted':>8}  {'ignored':>7}  {'undeliv':>7}  {'missed':>6}  {'acceptance':>10}  {'xcheck':>6}"
    lines = [header, "-" * len(header)]
    for r in rows:
        acc = f"{r['acceptance']:.0%}" if r["acceptance"] is not None else "—"
        xc = f"{r['xcheck']:.0%}" if r["xcheck"] is not None else "—"
        lines.append(
            f"{'v' + str(r['version']):>7}  {r['suggested']:>9}  {r['delivered']:>9}  "
            f"{r['accepted']:>8}  {r['rebutted']:>8}  {r['ignored']:>7}  "
            f"{r['undelivered']:>7}  {r['missed']:>6}  {acc:>10}  {xc:>6}"
        )
    flagged = rows[-1]["inconsistent"] if rows else []
    for o in flagged:
        lines.append(
            f"⚠ grade/{o['outcome']} disagrees with code signal "
            f"(file_touched={o.get('file_touched')}): {o.get('issue', '?')[:70]}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reflector.report", description=__doc__)
    ap.add_argument("repo", type=Path)
    args = ap.parse_args(argv)
    cc = args.repo.resolve() / ".codecouncil"
    suggestions = read_ndjson(cc / "suggestions.ndjsonl")
    outcomes = read_ndjson(cc / "outcomes.ndjsonl")
    rows = build_rows(suggestions, outcomes)
    if not rows:
        print("no data yet")
        return 0
    print(render(rows))

    has_rule_data = (
        any(s.get("verdict") == "SUGGESTION" and (s.get("suggestion") or {}).get("rule") is not None
            for s in suggestions)
        or any(o.get("rule") is not None for o in outcomes)
    )
    if has_rule_data:
        print()
        print(render_rule_rows(build_rule_rows(suggestions, outcomes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
