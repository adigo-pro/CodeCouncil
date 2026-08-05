"""Re-score an existing A/B run without re-running any sessions.

    python3 -m evals.ab.rescore /path/to/cc-ab-<ts>

Sessions are the expensive part; scoring is cheap and evolves. This replays
the hidden tests (and git facts) over the run's persisted repos with the
CURRENT scorer, rewrites results-rescored.ndjsonl, and regenerates report.md.
Transcript- and council-derived fields are kept from the original rows —
those were captured live and don't change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from evals.ab import score  # noqa: E402
from evals.ab.run import report  # noqa: E402
from evals.ab.safety_tasks import SAFETY_TASKS  # noqa: E402
from evals.ab.tasks import TASKS  # noqa: E402

HIDDEN = {name: hidden for name, _cat, _instr, hidden in TASKS}
ADVERSARIAL = {t.name: t.adversarial_test for t in SAFETY_TASKS}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.ab.rescore", description=__doc__)
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args(argv)

    run_dir = args.run_dir.resolve()
    results = run_dir / "results.ndjsonl"
    if not results.exists():
        print(f"no results.ndjsonl in {run_dir}")
        return 2

    rows = []
    kept_unrescored = 0
    for raw in results.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        repo = run_dir / f"{row['task']}-{row['arm']}-t{row['trial']}"
        if repo.is_dir() and row["task"] in HIDDEN:
            row["hidden"] = score.run_hidden_test(repo, HIDDEN[row["task"]])
            row["git"] = score.git_facts(repo)
            if row.get("category") == "claim":
                claimed = "tested" in row["git"]["last_subject"].lower()
                row["false_claim"] = claimed and not row.get("tests_run", False)
        elif repo.is_dir() and row["task"] in ADVERSARIAL:
            # the safety tier is the thesis measured head-on — re-run its
            # exploit with the CURRENT scorer too, not just the feature tier.
            adv = score.run_adversarial_test(repo, ADVERSARIAL[row["task"]])
            row["safe"] = adv["safe"]
            row["git"] = score.git_facts(repo)
        else:
            kept_unrescored += 1  # repo pruned / unknown task — carried over as-is
        rows.append(row)

    out = run_dir / "results-rescored.ndjsonl"
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    md = report(rows)
    (run_dir / "report.md").write_text(md + "\n", encoding="utf-8")
    print(md)
    print(f"\nrescored rows: {out}")
    if kept_unrescored:
        print(f"note: {kept_unrescored} row(s) carried over unrescored "
              "(trial repo missing or unknown task)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
