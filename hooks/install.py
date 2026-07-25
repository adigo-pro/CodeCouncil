"""Register the peer-review hook in a watched repo's .claude/settings.json.

    python3 -m hooks.install /path/to/repo

Idempotent merge: existing settings and hooks are preserved; running twice
changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.store import write_json_atomic

HOOK_SCRIPT = Path(__file__).resolve().parent / "peer_hook.py"
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"


def hook_command() -> str:
    return f"python3 {HOOK_SCRIPT}"


def merged_settings(settings: dict) -> tuple[dict, list[str]]:
    """Return (new settings, list of events actually added)."""
    cmd = hook_command()
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, matcher in (("PostToolUse", EDIT_MATCHER), ("Stop", None),
                           ("UserPromptSubmit", None)):
        entries = hooks.setdefault(event, [])
        already = any(
            h.get("command") == cmd
            for entry in entries
            for h in entry.get("hooks", [])
        )
        if already:
            continue
        entry: dict = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            entry["matcher"] = matcher
        entries.append(entry)
        added.append(event)
    return settings, added


def install(repo: Path) -> list[str]:
    settings_path = repo / ".claude" / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"error: {settings_path} exists but is not valid JSON — "
                  "leaving it untouched. Fix or remove it, then re-run.", file=sys.stderr)
            return []
        if not isinstance(settings, dict):
            print(f"error: {settings_path} does not contain a JSON object — "
                  "leaving it untouched. Fix or remove it, then re-run.", file=sys.stderr)
            return []
    settings, added = merged_settings(settings)
    if added:
        write_json_atomic(settings_path, settings, indent=2)
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hooks.install", description=__doc__)
    ap.add_argument("repo", type=Path, help="repo whose Claude Code sessions should receive suggestions")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2
    added = install(repo)
    if added:
        print(f"installed peer-review hook ({', '.join(added)}) in {repo / '.claude' / 'settings.json'}")
        print("note: running Claude Code sessions pick this up on restart")
    else:
        print("already installed — nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
