#!/usr/bin/env python3
"""Claude Code hook entry point for CodeCouncil peer review.

Registered (by hooks/install.py) as a PostToolUse and Stop hook in a watched
repo. Reads the hook event from stdin, checks .codecouncil/ in that repo for
undelivered Critic suggestions, and prints injection/block JSON to stdout.

Invariant: fail open. Whatever goes wrong, exit 0 with no output — this script
must never be able to break a developer's session.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks import ledger as ledger_mod
from hooks.logic import decide


def read_suggestions(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def run(stdin_text: str) -> str | None:
    event = json.loads(stdin_text)
    cc = Path(event["cwd"]) / ".codecouncil"
    suggestions_file = cc / "suggestions.ndjsonl"
    if not suggestions_file.exists():
        return None
    ledger_path = cc / "delivered.json"
    ledger = ledger_mod.load(ledger_path)
    output = decide(event, read_suggestions(suggestions_file), ledger, time.time())
    if output is None:
        return None
    ledger_mod.save(ledger_path, ledger)  # persist only when something was delivered
    return json.dumps(output)


def main() -> int:
    try:
        out = run(sys.stdin.read())
        if out:
            print(out)
    except Exception:
        pass  # fail open, always
    return 0


if __name__ == "__main__":
    sys.exit(main())
