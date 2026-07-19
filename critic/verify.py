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
STATUSES = {"VERIFIED": "verified", "REFUTED": "refuted", "INCONCLUSIVE": "inconclusive"}
_LINE_RE = re.compile(r"^(VERIFIED|REFUTED|INCONCLUSIVE)\s*[:—–-]\s*(.+)$", re.MULTILINE)


def build_prompt(suggestion: dict, sandbox_path: str) -> str:
    loc = f"{suggestion['file']}:{suggestion['line']}" if suggestion.get("line") else suggestion["file"]
    return (
        "TASK: VERIFY\n\n"
        f"FINDING: [{suggestion['severity'].upper()}] {loc} — {suggestion['issue']}\n"
        f"Rationale: {suggestion.get('rationale', '')}\n\n"
        f"The file under review is at: {sandbox_path}\n\n"
        "Write and RUN a minimal script that proves or refutes this finding "
        "against that file, then reply with exactly one line per your VERIFY "
        "protocol: VERIFIED: … | REFUTED: … | INCONCLUSIVE: …"
    )


def parse(raw: str) -> dict:
    matches = _LINE_RE.findall(raw.strip())
    if matches:
        status, note = matches[-1]
        return {"status": STATUSES[status], "note": note.strip()[:300]}
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
    return parse(reply)
