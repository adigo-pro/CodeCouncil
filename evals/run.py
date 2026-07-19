"""Frozen-case evaluation of every heuristics version — the controlled measure
of self-improvement. Same inputs each time; only the heuristics differ.

    python3 -m evals.run /path/to/repo        # evaluates v1..current of that repo
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from critic import openclaw, prompt  # noqa: E402
from observer.events import now_iso  # noqa: E402

CASES_DIR = Path(__file__).parent / "cases"


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]


def heuristics_versions(repo: Path) -> list[tuple[int, str]]:
    cc = repo / ".codecouncil"
    out = []
    history = cc / "heuristics-history"
    if history.is_dir():
        for f in sorted(history.glob("v*.md")):
            text = f.read_text(encoding="utf-8")
            out.append((prompt.heuristics_version(text), text))
    current = cc / "heuristics.md"
    if current.exists():
        text = current.read_text(encoding="utf-8")
        out.append((prompt.heuristics_version(text), text))
    return sorted({v: t for v, t in out}.items())


def score(case: dict, verdict: dict) -> bool:
    if case["expected"] == "pass":
        return verdict["verdict"] == "PASS"
    if verdict["verdict"] != "SUGGESTION":
        return False
    flagged = Path(verdict["suggestion"].get("file", "")).name
    return flagged in case["expect_files"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.run", description=__doc__)
    ap.add_argument("repo", type=Path, help="repo whose heuristics versions to evaluate")
    ap.add_argument("--sandbox", default="codecouncil")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    cases = load_cases()
    versions = heuristics_versions(repo)
    if not cases or not versions:
        print("no cases or no heuristics versions found")
        return 2
    results_path = repo / ".codecouncil" / "evals.ndjsonl"
    print(f"{len(cases)} frozen cases × {len(versions)} heuristics versions\n")

    rows = []
    for version, heur in versions:
        catches = clean = flag_total = pass_total = 0
        for case in cases:
            text = prompt.build_prompt(case["events"], case.get("latest_diff"), heur)
            try:
                verdict = prompt.parse_reply(openclaw.ask(
                    text, sandbox=args.sandbox, session=f"eval-{uuid.uuid4().hex[:10]}"))
            except openclaw.AgentError as e:
                verdict = {"verdict": "ERROR", "error": str(e)}
            ok = score(case, verdict)
            if case["expected"] == "flag":
                flag_total += 1
                catches += int(ok)
            else:
                pass_total += 1
                clean += int(ok)
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now_iso(), "version": version, "case": case["name"],
                                    "expected": case["expected"], "verdict": verdict["verdict"],
                                    "ok": ok}) + "\n")
            mark = "✓" if ok else "✗"
            print(f"  v{version} {mark} {case['name']:16} expected {case['expected']:4} "
                  f"got {verdict['verdict']}")
        rows.append((version, catches, flag_total, clean, pass_total))
        print()

    print(f"{'version':>7}  {'catches':>9}  {'clean passes':>13}  {'score':>6}")
    print("-" * 44)
    for version, catches, ft, clean, pt in rows:
        total = ft + pt
        print(f"{'v' + str(version):>7}  {catches:>5}/{ft:<3}  {clean:>9}/{pt:<3}  "
              f"{(catches + clean) / total:>6.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
