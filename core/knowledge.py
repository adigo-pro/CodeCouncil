"""Repo-specific facts distilled from rebutted findings — shared between the
Critic and Reflector loops.

CLAUDE.md's loop-boundary rule forbids the Critic (`critic/`) importing from
the Reflector (`reflector/`) or vice versa, except for a short list of small
shared utilities (`core.store`, `observer.events`, `observer.transcript`,
`critic.agent`). This module is an addition to that list: the Reflector
writes a fact here after each rebuttal it grades (`reflector/main.py`), and
the Critic reads the accumulated facts on every judgment
(`critic/main.py`) so the same rebuttal never has to recur. Since both loops
need it and neither owns it, it lives in `core/` rather than in either loop's
package — the same reasoning that put NDJSON plumbing in `core/store.py`.

`.codecouncil/knowledge.md` is the on-disk store: a capped, deduped bullet
list, atomically rewritten (temp file + os.replace in the same directory,
mirroring the heuristics.md swap in `reflector/rewrite.py`).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

KNOWLEDGE_MAX_FACTS = 30
MAX_FACT_CHARS = 240

HEADER = "# Repo knowledge (learned from past reviews)"

# A distilled "fact" ultimately traces back to developer/agent-controlled
# text (a rebuttal's evidence, itself grown from reasoning transcript
# content) that lands in critic prompts on every later judgment — a cheap
# directive filter so a planted "always/never <verdict verb>" sentence can
# never become a standing instruction. Not a general prompt-injection
# defense, just a floor: critic/persona.md's Discipline section is the
# actual backstop, told explicitly to treat knowledge entries as facts, never
# commands.
DIRECTIVE_RE = re.compile(r"(?i)\b(always|never)\s+(reply|respond|say|pass|approve|ignore)\b")


def build_distill_prompt(suggestion_row: dict, rebuttal_evidence: str) -> str:
    """One reflector TASK: DISTILL prompt: a rebutted finding plus the
    rebuttal evidence that grades it, asking for the one repo-specific fact
    (or NONE) that explains why the finding was wrong."""
    s = suggestion_row["suggestion"]
    loc = f"{s['file']}:{s['line']}" if s.get("line") else s["file"]
    return (
        "TASK: DISTILL\n\n"
        f"FINDING (delivered to the coding agent):\n"
        f"[{s.get('severity', 'medium').upper()}] {loc} — {s['issue']}\n"
        f"Rationale: {s.get('rationale', '')}\n\n"
        f"DEVELOPER'S REBUTTAL:\n{rebuttal_evidence}\n\n"
        "Reply with exactly ONE sentence stating the repo-specific fact or "
        "convention that makes the finding wrong (max 200 chars), or the "
        "single word NONE if the rebuttal reveals no reusable fact."
    )


def parse_fact(raw: str) -> str | None:
    """Strict parse of a TASK: DISTILL reply: strips whitespace, rejects
    NONE/empty/multi-line/over-length replies, and rejects anything reading
    as a directive (DIRECTIVE_RE) rather than a fact. Returns None for all of
    those, otherwise the fact sentence."""
    text = raw.strip()
    if not text or text.upper() == "NONE":
        return None
    if "\n" in text:
        return None
    if len(text) > MAX_FACT_CHARS:
        return None
    if DIRECTIVE_RE.search(text):
        return None
    return text


def _normalize(fact: str) -> str:
    # Trailing sentence punctuation is stripped so "Tests are X." and
    # "tests are x" (same fact, different terminal punctuation) dedupe.
    return re.sub(r"\s+", " ", fact.strip()).lower().rstrip(".!?")


def _facts(text: str) -> list[str]:
    return [line[2:].strip() for line in text.splitlines() if line.startswith("- ")]


def load(cc: Path) -> str:
    path = cc / "knowledge.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def add_fact(cc: Path, fact: str) -> bool:
    """Append `fact` to `.codecouncil/knowledge.md`, creating the file (with
    its header) if absent. Dedupes case-insensitively on normalized
    whitespace; once the cap is exceeded the oldest fact is evicted. Writes
    atomically (temp file + os.replace, same directory as the target — the
    established heuristics-swap pattern from reflector/rewrite.py).

    Returns True if the fact was newly added, False if it was a duplicate."""
    fact = fact.strip()
    if not fact:
        return False
    facts = _facts(load(cc))
    if _normalize(fact) in {_normalize(f) for f in facts}:
        return False
    facts.append(fact)
    if len(facts) > KNOWLEDGE_MAX_FACTS:
        facts = facts[-KNOWLEDGE_MAX_FACTS:]  # evict oldest first

    new_text = HEADER + "\n\n" + "\n".join(f"- {f}" for f in facts) + "\n"
    path = cc / "knowledge.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, path)
    return True
