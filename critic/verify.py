"""Prove findings before delivering them: the critic runs a repro before speaking.

A suggestion that arrives with 'VERIFIED: called safe_divide(1, 0), got
ZeroDivisionError' is a different product from a plausible guess — and a
REFUTED finding never reaches the developer at all.

The flagged file is staged into a throwaway directory and a tool-enabled pi
turn (read + bash) writes and runs a minimal repro there.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from core.redact import redact

from . import agent

# labels are about the FINDING, phrased so they cannot be read as being about
# the code's claim (a real 'refuted' once suppressed a true finding)
STATUSES = {
    "CONFIRMED": "verified", "FALSE-ALARM": "refuted", "INCONCLUSIVE": "inconclusive",
    "VERIFIED": "verified", "REFUTED": "refuted",  # legacy labels still parse
}
# Two accepted shapes for a status line: "[LABEL] <note>" (brackets — the
# separator after the bracket is optional) or "LABEL: <note>" (bare label —
# here the "[:—–-]" separator is REQUIRED, or a sentence like "Confirmed by
# reading the file, this is fine" would false-positive as a status line).
# Observed live: the verifier model replied "[CONFIRMED] ..." with no colon
# at all, which the old colon-only regex missed — a genuinely confirmed
# finding was stored "inconclusive".
_LINE_RE = re.compile(
    r"^(?:\[(CONFIRMED|FALSE-ALARM|INCONCLUSIVE|VERIFIED|REFUTED)\]"
    r"|(CONFIRMED|FALSE-ALARM|INCONCLUSIVE|VERIFIED|REFUTED)\s*[:—–-])\s*(.+)$",
    re.MULTILINE | re.IGNORECASE)

# A verified finding is delivered to a coding AGENT, not a human — a plain
# repro command it can run itself is worth more than another sentence of
# prose. Same bracket-tolerant two-form shape as _LINE_RE above: "[REPRO]
# <cmd>" (bracket, no separator required) or "REPRO: <cmd>" (bare label,
# separator required so ordinary prose mentioning "repro" doesn't match).
_REPRO_RE = re.compile(
    r"^(?:\[REPRO\]|REPRO\s*[:—–-])\s*(.+)$",
    re.MULTILINE | re.IGNORECASE)

# Delivered inline in hook-injected text (hooks/logic.py's _describe) — kept
# short for the same reason the status note is capped, using the same
# "… [N chars total]" marker as the rest of the codebase (observer/gitwatch.py,
# observer/transcript.py, critic/prompt.py).
REPRO_MAX_CHARS = 200

VERIFY_TOOLS = "read,bash,write,ls"


def build_prompt(suggestion: dict, staged_path: str) -> str:
    loc = f"{suggestion['file']}:{suggestion['line']}" if suggestion.get("line") else suggestion["file"]
    return (
        "TASK: VERIFY\n\n"
        f"FINDING: [{suggestion['severity'].upper()}] {loc} — {suggestion['issue']}\n"
        f"Rationale: {suggestion.get('rationale', '')}\n\n"
        f"The file under review is at: {staged_path}\n\n"
        "Write and RUN a minimal script that tests this finding against that "
        "file, then reply with exactly one line:\n"
        "CONFIRMED: <observed proof> — the problem is REAL (you reproduced the bad behavior)\n"
        "FALSE-ALARM: <why> — the code actually behaves correctly; the finding is wrong\n"
        "INCONCLUSIVE: <why> — cannot be tested in isolation\n\n"
        "If CONFIRMED, add a second line:\n"
        "REPRO: <one shell command, runnable from the repo root, that demonstrates the problem>"
    )


def _cap(text: str, limit: int) -> str:
    """Truncate with the same '… [N chars total]' marker used elsewhere in
    the codebase (observer/transcript.py, observer/gitwatch.py, critic/prompt.py)."""
    return text if len(text) <= limit else text[:limit] + f"… [{len(text)} chars total]"


def localize_repro(repro: str, staging: Path) -> str:
    """Best-effort: the verifier only ever sees the throwaway staging copy of
    the file (an absolute tempdir path meaningless outside that sandbox), so
    rewrite any mention of the staging dir back to a repo-root-relative '.'
    — the repro command a receiving agent copy-pastes must run from the
    repo it's actually working in."""
    return repro.replace(str(staging), ".")


def parse(raw: str) -> dict:
    stripped = raw.strip()
    matches = _LINE_RE.findall(stripped)
    if matches:
        bracket_label, colon_label, note = matches[-1]
        status = (bracket_label or colon_label).upper()
        result = {"status": STATUSES[status], "note": note.strip()[:300]}
    else:
        result = {"status": "inconclusive", "note": f"unparseable verify reply: {raw[:200]}"}
    repro_matches = _REPRO_RE.findall(stripped)
    if repro_matches:
        result["repro"] = _cap(redact(repro_matches[-1].strip()), REPRO_MAX_CHARS)
    return result


def verify_finding(repo: Path, suggestion: dict, system: str | None = None) -> dict:
    """Returns {"status": verified|refuted|inconclusive|error, "note": str}."""
    local = repo / suggestion.get("file", "")
    if not local.is_file():
        return {"status": "inconclusive", "note": "flagged file not found in repo"}
    # a throwaway staging dir: repro runs never touch the developer's repo, and
    # nothing is left behind for the critic to later flag as a finding
    staging = Path(tempfile.mkdtemp(prefix="codecouncil-verify-"))
    try:
        staged = staging / local.name
        shutil.copyfile(local, staged)
        reply = agent.ask(build_prompt(suggestion, str(staged)), system=system,
                          tools=VERIFY_TOOLS, cwd=str(staging))
    except agent.AgentError as e:
        return {"status": "error", "note": str(e)[:200]}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result = parse(reply)
    if "repro" in result:
        result["repro"] = localize_repro(result["repro"], staging)
    return result
