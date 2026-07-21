"""Rewrite heuristics.md from graded outcomes: prompt, validation, atomic swap."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

MIN_NEW_OUTCOMES = 3
MAX_LINES = 40

GRADED = {"accepted", "rebutted", "ignored"}


def rules(text: str) -> list[str]:
    """Top-level `- ` bullets with continuation lines joined."""
    out: list[str] = []
    for line in text.split("\n"):
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif line.startswith("  ") and line.strip() and out:
            out[-1] += " " + line.strip()
    return out


def rewrite_record(old_text: str, new_text: str, version: int, outcomes: list[dict]) -> dict:
    """Ledger entry describing what a rewrite changed and what data drove it."""
    old_rules, new_rules = set(rules(old_text)), rules(new_text)
    added = [r for r in new_rules if r not in old_rules]
    removed = [r for r in rules(old_text) if r not in set(new_rules)]
    return {
        "from_version": version,
        "to_version": version + 1,
        "stats": {g: sum(1 for o in outcomes if o.get("outcome") == g) for g in GRADED},
        "added": added,
        "removed": removed,
        "headline": added[0] if added else (f"dropped: {removed[0]}" if removed else "reworded"),
    }


def should_rewrite(outcomes: list[dict], n_graded_at_last_rewrite: int, force: bool,
                   min_new: int = MIN_NEW_OUTCOMES) -> bool:
    graded = sum(1 for o in outcomes if o.get("outcome") in GRADED)
    return force or graded - n_graded_at_last_rewrite >= min_new


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


def gate_candidate(candidate_text: str, current_text: str) -> tuple[bool, str]:
    """Score a rewrite candidate against the current heuristics on the frozen
    eval cases before it's allowed to land — format-valid isn't good enough,
    it has to not be a regression. Same cases, same stub, one model call per
    case per side. No eval cases at all => ungated (pass through).

    Cost note: this is 2×len(cases) real model calls per attempt (candidate
    + current, one call per case each) — not cheap if the eval set grows
    large. reflector.main.maybe_rewrite bounds how often that fan-out can
    fire by backing off (state["n_graded_at_last_rewrite"]) after any
    rejection here, so a rejected candidate doesn't get regenerated and
    re-scored every reflector pass against the same stale outcomes."""
    from evals.run import load_cases, score_heuristics  # lazy: keep import graph clean

    cases = load_cases()
    if not cases:
        return True, "no eval cases — ungated"
    candidate = score_heuristics(candidate_text, cases)
    current = score_heuristics(current_text, cases)
    note = f"candidate {candidate['score']:.2f} vs current {current['score']:.2f}"
    return candidate["score"] >= current["score"], note


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
