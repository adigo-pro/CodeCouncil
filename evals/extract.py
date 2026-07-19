"""One-time case extractor: freeze real observation payloads into eval cases.

    python3 -m evals.extract /path/to/training-repo

Each case is a real commit event (plus the nearest diff snapshot) with a known
expected verdict. Cases are frozen JSON — the eval replays them against any
heuristics version with identical inputs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CASES_DIR = Path(__file__).parent / "cases"

# beat of the source commit -> (case name, expected, acceptable flagged files)
WANTED = {
    105: ("latent-config", "flag", ["config.py"]),
    42: ("latent-drift", "flag", ["netutil.py"]),
    177: ("docstring-trap", "flag", ["strutil.py", "cli.py"]),
    11: ("clean-slugify", "pass", []),
    76: ("clean-truncate", "pass", []),
    333: ("clean-rename", "pass", []),
}


def main() -> int:
    repo = Path(sys.argv[1]).resolve()
    obs = [json.loads(l) for l in
           (repo / ".codecouncil" / "observations.ndjsonl").read_text().splitlines()]
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for e in obs:
        if e["type"] != "commit" or e["beat"] not in WANTED:
            continue
        name, expected, files = WANTED[e["beat"]]
        diffs_after = [d for d in obs if d["type"] == "diff" and d["beat"] >= e["beat"]]
        case = {
            "name": name,
            "expected": expected,
            "expect_files": files,
            "events": [e],
            "latest_diff": diffs_after[0] if diffs_after else None,
        }
        (CASES_DIR / f"{name}.json").write_text(json.dumps(case, indent=1), encoding="utf-8")
        print(f"froze {name} (beat {e['beat']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
