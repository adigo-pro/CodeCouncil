"""Rewrite heuristics.md from graded outcomes: prompt, validation, atomic swap."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

MIN_NEW_OUTCOMES = 3
MAX_LINES = 40

GRADED = {"accepted", "rebutted", "ignored"}


def should_rewrite(outcomes: list[dict], n_graded_at_last_rewrite: int, force: bool) -> bool:
    graded = sum(1 for o in outcomes if o.get("outcome") in GRADED)
    return force or graded - n_graded_at_last_rewrite >= MIN_NEW_OUTCOMES


def build_prompt(current: str, version: int, outcomes: list[dict]) -> str:
    lines = []
    for o in outcomes:
        if o.get("outcome") not in GRADED:
            continue
        lines.append(f"- [{o['outcome'].upper()}] {o.get('issue', '?')}"
                     + (f" — {o['evidence']}" if o.get("evidence") else ""))
    counts = {g: sum(1 for o in outcomes if o.get("outcome") == g) for g in GRADED}
    return (
        "TASK: REWRITE HEURISTICS\n\n"
        f"CURRENT FILE (version {version}):\n{current.strip()}\n\n"
        f"GRADED OUTCOMES OF SUGGESTIONS MADE UNDER THESE HEURISTICS:\n"
        + ("\n".join(lines) or "(none)")
        + f"\n\nSTATS: accepted={counts['accepted']} rebutted={counts['rebutted']} "
        f"ignored={counts['ignored']}\n\n"
        f"Produce version {version + 1} per your REWRITE protocol: output only the "
        f"complete new file, first line exactly 'version: {version + 1}', max {MAX_LINES} lines."
    )


def validate(new_text: str, expected_version: int) -> str | None:
    """Return an error string, or None if the rewrite is acceptable."""
    text = new_text.strip()
    if not text:
        return "empty output"
    if text.startswith("```"):
        return "fenced output"
    first = text.splitlines()[0].strip()
    if not re.fullmatch(rf"version:\s*{expected_version}", first):
        return f"first line is {first!r}, expected 'version: {expected_version}'"
    if len(text.splitlines()) > MAX_LINES:
        return f"too long ({len(text.splitlines())} lines > {MAX_LINES})"
    return None


def apply(heuristics_path: Path, new_text: str, old_text: str, old_version: int) -> Path:
    """Archive the old version, then atomically swap in the new file."""
    history = heuristics_path.parent / "heuristics-history"
    history.mkdir(parents=True, exist_ok=True)
    archive = history / f"v{old_version}.md"
    archive.write_text(old_text, encoding="utf-8")

    fd, tmp = tempfile.mkstemp(dir=heuristics_path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text.strip() + "\n")
    os.replace(tmp, heuristics_path)
    return archive
