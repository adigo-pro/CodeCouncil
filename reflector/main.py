"""CodeCouncil Reflector: slow heartbeat that grades past suggestions against
what actually happened, then rewrites the Critic's heuristics from the grades.

    python3 -m reflector /path/to/watched/repo [--interval 300] [--once] [--force-rewrite]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from core.store import append_row as append_ndjson
from core.store import read_rows as read_ndjson
from core.store import read_tail_rows, wait_for
from critic import agent
from critic.main import ensure_heuristics
from critic.prompt import heuristics_version
from hooks import ledger as ledger_mod
from observer.events import now_iso

from . import judge, rewrite

PERSONA = Path(__file__).parent / "persona.md"


def _ask(prompt: str) -> str:
    return agent.ask(prompt, system=PERSONA.read_text(encoding="utf-8"))


def grade_pending(cc: Path) -> int:
    # bounded: only the recent tail can hold newly-gradeable suggestions
    suggestions = read_tail_rows(cc / "suggestions.ndjsonl")
    delivered = ledger_mod.load(cc / "delivered.json")
    outcomes_path = cc / "outcomes.ndjsonl"
    # unbounded: graded_ids is a dedup set — losing old ids would re-grade
    # old suggestions, so this one must see the whole file
    graded_ids = {o["suggestion_id"] for o in read_ndjson(outcomes_path)}
    # only recent observations matter here (evidence windows reach back minutes)
    observations = read_tail_rows(cc / "observations.ndjsonl")
    now = time.time()

    to_judge, undelivered = judge.pending(suggestions, delivered, graded_ids, now)

    for row in undelivered:
        append_ndjson(outcomes_path, {
            "id": uuid.uuid4().hex[:12], "suggestion_id": row["id"], "ts": now_iso(),
            "outcome": "undelivered", "issue": row["suggestion"]["issue"],
            "heuristics_version": row.get("heuristics_version", 0),
        })
        print(f"reflector: {row['id']} → undelivered (no model call)")

    for row in to_judge:
        d = judge.first_delivery(delivered, row["id"])
        reason = judge.explicit_rebuttal(d, observations)
        if reason is not None:
            grade = {"outcome": "rebutted", "evidence": f"explicit rebuttal: {reason}"}
        else:
            prompt = judge.build_prompt(row, judge.evidence(row, d, observations))
            try:
                grade = judge.parse_grade(_ask(prompt))
            except agent.AgentError as e:
                print(f"reflector: grading {row['id']} failed ({e}); will retry next beat")
                continue
        append_ndjson(outcomes_path, {
            "id": uuid.uuid4().hex[:12], "suggestion_id": row["id"], "ts": now_iso(),
            "outcome": grade["outcome"], "evidence": grade.get("evidence", ""),
            "issue": row["suggestion"]["issue"],
            "heuristics_version": row.get("heuristics_version", 0),
            "file_touched": judge.file_touched(row, d, observations),
            **({"malformed": grade["malformed"]} if "malformed" in grade else {}),
        })
        print(f"reflector: {row['id']} → {grade['outcome']}"
              + (f" ({grade['evidence']})" if grade.get("evidence") else ""))

    return len(to_judge) + len(undelivered)


def maybe_rewrite(cc: Path, state: dict, force: bool,
                  rewrite_after: int = rewrite.MIN_NEW_OUTCOMES) -> None:
    outcomes = read_ndjson(cc / "outcomes.ndjsonl")
    if not rewrite.should_rewrite(outcomes, state.get("n_graded_at_last_rewrite", 0), force,
                                  min_new=rewrite_after):
        return
    heuristics_path = cc / "heuristics.md"
    current = ensure_heuristics(heuristics_path)  # seeds v1 if the critic hasn't yet
    version = heuristics_version(current)
    prompt = rewrite.build_prompt(current, version, outcomes)
    try:
        new_text = _ask(prompt)
    except agent.AgentError as e:
        print(f"reflector: rewrite failed ({e}); keeping v{version}")
        return
    err = rewrite.validate(new_text, version + 1)
    if err:
        print(f"reflector: rewrite rejected ({err}); keeping v{version}")
        return
    archive = rewrite.apply(heuristics_path, new_text, current, version)
    state["n_graded_at_last_rewrite"] = sum(
        1 for o in outcomes if o.get("outcome") in rewrite.GRADED
    )
    append_ndjson(cc / "reflections.ndjsonl",
                  {"ts": now_iso(), **rewrite.rewrite_record(current, new_text, version, outcomes)})
    print(f"reflector: heuristics v{version} → v{version + 1} (archived {archive.name})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reflector", description=__doc__)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--interval", type=float, default=300.0, help="seconds between passes (default 300)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--force-rewrite", action="store_true",
                    help="rewrite even below the outcome threshold (demo)")
    ap.add_argument("--rewrite-after", type=int, default=rewrite.MIN_NEW_OUTCOMES,
                    help="graded outcomes needed to trigger a rewrite (default 3)")
    args = ap.parse_args(argv)

    cc = args.repo.resolve() / ".codecouncil"
    if not wait_for(cc / "suggestions.ndjsonl", "has the critic run?", args.once, poll_s=5):
        return 2

    state_path = cc / "reflector-state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    print(f"reflector: watching {cc} · every {args.interval:g}s")
    try:
        while True:
            n = grade_pending(cc)
            if n == 0:
                print(f"reflector: nothing to grade")
            maybe_rewrite(cc, state, args.force_rewrite,
                          rewrite_after=args.rewrite_after)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nreflector: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
