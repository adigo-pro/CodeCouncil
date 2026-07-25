"""A/B evaluation: Claude Code WITH CodeCouncil vs WITHOUT, on identical tasks
with hidden acceptance tests.

    python3 -m evals.ab.run [--trials N] [--tasks K] [--arms both|with|without]

Paired design: every (task, trial) runs once per arm in its own fresh scratch
repo seeded with the same latent-trap files (training.run.SEED_FILES). The
'with' arm gets hooks + observer + critic (the delivery loop — the treatment);
the 'without' arm is a bare repo. Hidden tests (evals.ab.tasks) score the
result; the agent never sees them. Rows land in results.ndjsonl; a markdown
report is printed and written at the end.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from evals.ab import score  # noqa: E402
from evals.ab.tasks import TASKS  # noqa: E402
from hooks.install import install as install_hooks  # noqa: E402
from observer.transcript import find_project_dir  # noqa: E402
from training.run import SEED_FILES, sh  # noqa: E402

SESSION_TIMEOUT = 420
SETTLE_SECONDS = 25  # let the critic judge the session's final diff


def seed_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for name, content in SEED_FILES.items():
        (repo / name).write_text(content, encoding="utf-8")
    sh(["git", "init", "-qb", "main"], cwd=repo)
    sh(["git", "add", "-A"], cwd=repo)
    sh(["git", "commit", "-qm", "seed demoapp"], cwd=repo)


def start_council(repo: Path, probes: bool = False) -> list[subprocess.Popen]:
    install_hooks(repo)
    cc = repo / ".codecouncil"
    cc.mkdir(exist_ok=True)

    def spawn(mod: str, *flags: str) -> subprocess.Popen:
        log = (cc / f"{mod}.live.log").open("a")
        return subprocess.Popen([sys.executable, "-u", "-m", mod, str(repo), *flags],
                                cwd=REPO_ROOT, stdout=log, stderr=log)

    # eval-profile timing: round 1's --interval 10 --turn-spacing 20 left the
    # critic too slow relative to these (short) sessions to ever get a turn
    # in; tightened so the critic actually gets a look before the session ends.
    critic_flags = ["--interval", "5", "--turn-spacing", "10"]
    if probes:
        critic_flags.append("--probes")
    return [spawn("observer", "--wait"),
            spawn("critic", *critic_flags)]


def run_session(repo: Path, instruction: str) -> dict:
    t0 = time.time()
    for attempt in (1, 2):
        r = sh(["claude", "-p", instruction, "--permission-mode", "acceptEdits",
                "--allowedTools", "Edit", "Write", "Bash"], cwd=repo,
               timeout=SESSION_TIMEOUT)
        if r.returncode == 0:
            break
        time.sleep(30 * attempt)
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 1),
            "error": "" if r.returncode == 0 else (r.stderr or r.stdout)[-300:]}


def run_trial(base: Path, name: str, category: str, instruction: str,
              hidden: str, arm: str, trial: int, probes: bool = False) -> dict:
    repo = base / f"{name}-{arm}-t{trial}"
    seed_repo(repo)
    daemons = start_council(repo, probes=probes) if arm == "with" else []
    if daemons:
        time.sleep(3)
    try:
        session = run_session(repo, instruction)
        if daemons:
            time.sleep(SETTLE_SECONDS)
    finally:
        for p in daemons:
            p.terminate()

    hidden_result = score.run_hidden_test(repo, hidden)
    project_dir = find_project_dir(repo)
    commands = score.bash_commands_from_transcript(project_dir) if project_dir else []
    git = score.git_facts(repo)
    row = {"task": name, "category": category, "arm": arm, "trial": trial,
           "session": session, "hidden": hidden_result,
           "tests_run": score.tests_run(commands), "bash_commands": len(commands),
           "git": git}
    if arm == "with":
        row["council"] = score.council_stats(repo)
    if category == "claim":
        claimed = "tested" in git["last_subject"].lower()
        row["false_claim"] = claimed and not row["tests_run"]
    return row


def report(rows: list[dict]) -> str:
    lines = ["| task | arm | hidden | tests run | notes |",
             "|---|---|---|---|---|"]
    totals: dict[str, list[float]] = {"with": [], "without": []}
    for r in rows:
        h = r["hidden"]
        frac = f"{h['passed']}/{h['total']}" if h["total"] else "crash"
        if h["total"]:
            totals[r["arm"]].append(h["passed"] / h["total"])
        notes = []
        if r.get("false_claim"):
            notes.append("FALSE CLAIM")
        if r.get("council"):
            c = r["council"]
            notes.append(f"{c['findings']} finding(s), {c['receipts']} receipt(s)")
        if r["session"]["rc"] != 0:
            notes.append(f"session rc={r['session']['rc']}")
        lines.append(f"| {r['task']} | {r['arm']} | {frac} | "
                     f"{'yes' if r['tests_run'] else 'no'} | {'; '.join(notes)} |")
    for arm in ("without", "with"):
        vals = totals[arm]
        mean = sum(vals) / len(vals) if vals else 0.0
        lines.append("")
        lines.append(f"**{arm} council:** mean hidden-test pass rate "
                     f"{mean:.0%} over {len(vals)} trials")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="evals.ab.run", description=__doc__)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--tasks", type=int, default=None, help="first K tasks only")
    ap.add_argument("--arms", choices=["both", "with", "without"], default="both")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--probes", action="store_true",
                    help="enable critic property probes on the with-council arm "
                         "(default off)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    tasks = TASKS[:args.tasks] if args.tasks else TASKS
    arms = ["without", "with"] if args.arms == "both" else [args.arms]
    base = (args.out or Path.home() / "tmp" / f"cc-ab-{int(time.time())}").resolve()
    base.mkdir(parents=True, exist_ok=True)
    results = base / "results.ndjsonl"
    n_total = len(tasks) * len(arms) * args.trials
    print(f"A/B eval: {len(tasks)} tasks × {arms} × {args.trials} trial(s) "
          f"= {n_total} sessions → {base}")

    rows = []
    done = 0
    for trial in range(1, args.trials + 1):
        for name, category, instruction, hidden in tasks:
            for arm in arms:
                done += 1
                print(f"[{done}/{n_total}] {name} · {arm} · trial {trial} …",
                      flush=True)
                row = run_trial(base, name, category, instruction, hidden,
                                arm, trial, probes=args.probes)
                rows.append(row)
                with results.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                h = row["hidden"]
                print(f"    hidden {h['passed']}/{h['total']} · "
                      f"tests_run={row['tests_run']} · "
                      f"{row['session']['seconds']}s", flush=True)

    md = report(rows)
    (base / "report.md").write_text(md + "\n", encoding="utf-8")
    print("\n" + md)
    print(f"\nrows: {results}\nreport: {base / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
