"""Frozen-case evaluation of every heuristics version — the controlled measure
of self-improvement. Same inputs each time; only the heuristics differ.

    python3 -m evals.run /path/to/repo        # evaluates v1..current of that repo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from critic import agent, prompt  # noqa: E402
from observer.events import now_iso  # noqa: E402

CASES_DIR = Path(__file__).parent / "cases"
# Auto-grown by reflector.harvest.maybe_harvest from real graded outcomes —
# sibling of CASES_DIR, loaded after it so hand-made cases always sort first.
HARVESTED_CASES_DIR = Path(__file__).parent / "cases-harvested"


def load_cases() -> list[dict]:
    paths = sorted(CASES_DIR.glob("*.json"))
    if HARVESTED_CASES_DIR.is_dir():
        paths += sorted(HARVESTED_CASES_DIR.glob("*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


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


def score_heuristics(heuristics_text: str, cases: list[dict] | None = None) -> dict:
    """Score one heuristics text against frozen cases — one model call per case.
    The controlled comparison the rewrite gate and `python3 -m evals.run` both
    build on: same inputs, only the heuristics text differs.

    Returns {"score": 0..1, "n": len(cases), "results": [{"case", "expected",
    "verdict", "ok"}, ...]}.
    """
    if cases is None:
        cases = load_cases()
    persona = agent.CRITIC_PERSONA.read_text(encoding="utf-8")
    results = []
    correct = 0
    for case in cases:
        text = prompt.build_prompt(case["events"], case.get("latest_diff"), heuristics_text)
        try:
            verdict = prompt.parse_reply(agent.ask(text, system=persona))
        except agent.AgentError as e:
            verdict = {"verdict": "ERROR", "error": str(e)}
        ok = score(case, verdict)
        correct += int(ok)
        results.append({"case": case["name"], "expected": case["expected"],
                        "verdict": verdict["verdict"], "ok": ok})
    n = len(cases)
    return {"score": (correct / n) if n else 0.0, "n": n, "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.run", description=__doc__)
    ap.add_argument("repo", type=Path, help="repo whose heuristics versions to evaluate")
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
        scored = score_heuristics(heur, cases)
        for case, r in zip(cases, scored["results"]):
            ok = r["ok"]
            if case["expected"] == "flag":
                flag_total += 1
                catches += int(ok)
            else:
                pass_total += 1
                clean += int(ok)
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now_iso(), "version": version, "case": r["case"],
                                    "expected": r["expected"], "verdict": r["verdict"],
                                    "ok": ok}) + "\n")
            mark = "✓" if ok else "✗"
            print(f"  v{version} {mark} {r['case']:16} expected {r['expected']:4} "
                  f"got {r['verdict']}")
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
