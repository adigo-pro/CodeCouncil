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

from . import judge, report, rewrite

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
    graded_now = sum(1 for o in outcomes if o.get("outcome") in rewrite.GRADED)
    err = rewrite.validate(new_text, version + 1)
    if err:
        print(f"reflector: rewrite rejected ({err}); keeping v{version}")
        # No new signal will appear until fresh grades land — without this,
        # every pass would regenerate a candidate from the same stale
        # outcomes and hit this same rejection forever.
        state["n_graded_at_last_rewrite"] = graded_now
        return
    gate_ok, gate_note = rewrite.gate_candidate(new_text, current)
    if not gate_ok:
        print(f"reflector: rewrite rejected by eval gate ({gate_note}); keeping v{version}")
        append_ndjson(cc / "reflections.ndjsonl", {
            "ts": now_iso(), "event": "rewrite_rejected",
            "from_version": version, "note": gate_note,
        })
        # Same reasoning as the validate() failure above: back off until
        # MIN_NEW_OUTCOMES fresh grades accumulate, instead of re-running
        # 2×len(cases) model calls every pass against unchanged outcomes.
        state["n_graded_at_last_rewrite"] = graded_now
        return
    archive = rewrite.apply(heuristics_path, new_text, current, version)
    state["n_graded_at_last_rewrite"] = graded_now
    record = rewrite.rewrite_record(current, new_text, version, outcomes)
    append_ndjson(cc / "reflections.ndjsonl", {"ts": now_iso(), "gate": gate_note, **record})
    print(f"reflector: heuristics v{version} → v{version + 1} "
          f"(archived {archive.name}, gate: {gate_note})")


def maybe_rollback(cc: Path, state: dict, outcomes: list[dict]) -> None:
    """Auto-revert: if the current heuristics version is measurably worse in
    the wild than the version it replaced, restore the old rules (as a new,
    higher version number — version numbers never go backwards) and archive
    the underperformer. Compares in-the-wild acceptance (reflector.report),
    never the frozen-eval score the rewrite gate uses — different signals."""
    heuristics_path = cc / "heuristics.md"
    if not heuristics_path.exists():
        return
    current_text = heuristics_path.read_text(encoding="utf-8")
    current_version = heuristics_version(current_text)
    guard_key = f"rolled_back_from_{current_version}"
    if state.get(guard_key):
        return

    prev_version = current_version - 1
    history = cc / "heuristics-history"
    if not history.is_dir():
        return
    archive_files = sorted(history.glob(f"v{prev_version}.md"))
    if not archive_files:
        return
    prev_archive = archive_files[-1]

    suggestions = read_ndjson(cc / "suggestions.ndjsonl")
    rows = {r["version"]: r for r in report.build_rows(suggestions, outcomes)}
    current_row, prev_row = rows.get(current_version), rows.get(prev_version)
    if current_row is None or prev_row is None:
        return
    graded_current = current_row["accepted"] + current_row["rebutted"] + current_row["ignored"]
    if graded_current < rewrite.MIN_NEW_OUTCOMES:
        return
    if current_row["acceptance"] is None or prev_row["acceptance"] is None:
        return
    if not (current_row["acceptance"] < prev_row["acceptance"]):
        return

    restored = prev_archive.read_text(encoding="utf-8").strip().splitlines()
    new_version = current_version + 1
    restored[0] = f"version: {new_version}"
    new_text = "\n".join(restored)

    archive = rewrite.apply(heuristics_path, new_text, current_text, current_version)
    append_ndjson(cc / "reflections.ndjsonl", {
        "ts": now_iso(), "event": "rollback",
        "from_version": current_version, "restored_version_content_of": prev_version,
        "note": f"v{current_version} acceptance {current_row['acceptance']:.0%} < "
                f"v{prev_version} acceptance {prev_row['acceptance']:.0%}",
    })
    state[guard_key] = True
    # Same fix as maybe_rewrite's post-apply update: the outcomes that
    # triggered this revert are stale evidence against the restored
    # version — without advancing this, maybe_rewrite (called right after
    # maybe_rollback in the same pass) would immediately try to rewrite the
    # just-restored rules using the very outcomes that condemned the old v2.
    state["n_graded_at_last_rewrite"] = sum(
        1 for o in outcomes if o.get("outcome") in rewrite.GRADED
    )
    print(f"reflector: auto-rollback v{current_version} → v{new_version} "
          f"(restoring rules of v{prev_version}, archived {archive.name})")


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
            maybe_rollback(cc, state, read_ndjson(cc / "outcomes.ndjsonl"))
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
