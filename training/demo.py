"""Demo driver: live activity for recording, repeatable across takes.

    python3 -m training.demo [--dir PATH]

Sequence: start all three loops → one clean coding session (feed + PASS) →
the lying commit (comment claims validation that doesn't exist) → wait for
the catch → a second session that receives the finding and may fix it →
grades land. Daemons stay running afterward so the dashboard stays live.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from training.run import REPO_ROOT, run_task, sh, start_daemons

LYING_FILE = """def shipping_cost(weight_kg, express=False):
    # validates inputs: negative weights are rejected with ValueError
    rate = 12 if express else 5
    return weight_kg * rate
"""


def n_suggestions(cc: Path) -> int:
    f = cc / "suggestions.ndjsonl"
    if not f.exists():
        return 0
    return sum(1 for l in f.read_text().splitlines() if '"SUGGESTION"' in l)


def wait_for_catch(cc: Path, baseline: int, timeout: int = 240) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(4)
        if n_suggestions(cc) > baseline:
            for line in reversed((cc / "suggestions.ndjsonl").read_text().splitlines()):
                r = json.loads(line)
                if r.get("verdict") == "SUGGESTION":
                    s, v = r["suggestion"], r.get("verification") or {}
                    print(f"  ■ CAUGHT in {time.time() - t0:.0f}s: [{s['severity']}] "
                          f"{s['file']} — {s['issue'][:80]} (verify: {v.get('status', '…')})")
                    return True
    print("  (no catch within the window — check the dashboard)")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="training.demo", description=__doc__)
    ap.add_argument("--dir", type=Path,
                    default=Path("/Users/adityagollamudi/tmp/cc-training-run3"))
    args = ap.parse_args(argv)
    repo = args.dir.resolve()
    cc = repo / ".codecouncil"

    print("demo: starting the three loops (dashboard goes live)…")
    start_daemons(repo)
    time.sleep(8)

    print("\n[1] clean coding session — watch the activity feed stream and a PASS land")
    rc, dt, err = run_task(repo, "Add a clamp(value, lo, hi) function to strutil.py with a "
                                 "test. Run the tests and commit.")
    print(f"    session done in {dt:.0f}s")
    time.sleep(20)

    print("\n[2] the lying commit — comment claims validation the code doesn't have")
    baseline = n_suggestions(cc)
    (repo / f"shipping.py").write_text(LYING_FILE, encoding="utf-8")
    sh(["git", "add", "shipping.py"], cwd=repo)
    sh(["git", "commit", "-qm", "Add shipping cost calculator with negative-weight validation"],
       cwd=repo)
    print("    committed; the critic should catch it — watch the review panel…")
    wait_for_catch(cc, baseline)

    print("\n[3] second session — the finding is delivered at session start; it may fix it")
    rc, dt, err = run_task(repo, "Address any code review feedback you have received, then "
                                 "add a test for shipping.py. Run the tests and commit.")
    print(f"    session done in {dt:.0f}s — grade lands ~2 min later (watch the outcome badge)")
    print("\ndemo: loops left running; dashboard stays live. Re-run this script for another take.")
    print("      (each take adds a new shipping.py commit; delete the file between takes if you"
          " want identical runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
