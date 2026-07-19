"""Prove findings before delivering them: the critic runs a repro in its sandbox.

A suggestion that arrives with 'VERIFIED: called safe_divide(1, 0), got
ZeroDivisionError' is a different product from a plausible guess — and a
REFUTED finding never reaches the developer at all.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from . import openclaw

UNDER_REVIEW = "/sandbox/workspaces/critic/underreview"
# labels are about the FINDING, phrased so they cannot be read as being about
# the code's claim (a real 'refuted' once suppressed a true finding)
STATUSES = {
    "CONFIRMED": "verified", "FALSE-ALARM": "refuted", "INCONCLUSIVE": "inconclusive",
    "VERIFIED": "verified", "REFUTED": "refuted",  # legacy labels still parse
}
_LINE_RE = re.compile(
    r"^(CONFIRMED|FALSE-ALARM|INCONCLUSIVE|VERIFIED|REFUTED)\s*[:—–-]\s*(.+)$",
    re.MULTILINE | re.IGNORECASE)


def build_prompt(suggestion: dict, sandbox_path: str) -> str:
    loc = f"{suggestion['file']}:{suggestion['line']}" if suggestion.get("line") else suggestion["file"]
    return (
        "TASK: VERIFY\n\n"
        f"FINDING: [{suggestion['severity'].upper()}] {loc} — {suggestion['issue']}\n"
        f"Rationale: {suggestion.get('rationale', '')}\n\n"
        f"The file under review is at: {sandbox_path}\n\n"
        "Write and RUN a minimal script that tests this finding against that "
        "file, then reply with exactly one line:\n"
        "CONFIRMED: <observed proof> — the problem is REAL (you reproduced the bad behavior)\n"
        "FALSE-ALARM: <why> — the code actually behaves correctly; the finding is wrong\n"
        "INCONCLUSIVE: <why> — cannot be tested in isolation"
    )


def parse(raw: str) -> dict:
    matches = _LINE_RE.findall(raw.strip())
    if matches:
        status, note = matches[-1]
        return {"status": STATUSES[status.upper()], "note": note.strip()[:300]}
    return {"status": "inconclusive", "note": f"unparseable verify reply: {raw[:200]}"}


def _push_file(local: Path, sandbox: str, sandbox_path: str) -> bool:
    try:
        res = subprocess.run(
            ["nemoclaw", sandbox, "exec", "--stdin", "--", "bash", "-c",
             f"mkdir -p {UNDER_REVIEW} && cat > {sandbox_path}"],
            input=local.read_bytes(), capture_output=True, timeout=60,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def verify_finding(repo: Path, suggestion: dict, sandbox: str, agent: str) -> dict:
    """Returns {"status": verified|refuted|inconclusive|error, "note": str}."""
    local = repo / suggestion.get("file", "")
    if not local.is_file():
        return {"status": "inconclusive", "note": "flagged file not found in repo"}
    sandbox_path = f"{UNDER_REVIEW}/{uuid.uuid4().hex[:8]}-{local.name}"
    if not _push_file(local, sandbox, sandbox_path):
        return {"status": "error", "note": "could not stage file in sandbox"}
    try:
        reply = openclaw.ask(build_prompt(suggestion, sandbox_path), sandbox=sandbox,
                             agent=agent, session=f"verify-{uuid.uuid4().hex[:12]}")
    except openclaw.AgentError as e:
        return {"status": "error", "note": str(e)[:200]}
    finally:
        # never leave staging copies behind — the critic once flagged its own
        # stale underreview/ file as a finding in the watched repo
        try:
            subprocess.run(["nemoclaw", sandbox, "exec", "--", "rm", "-f", sandbox_path],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return parse(reply)
