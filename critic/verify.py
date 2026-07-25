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

# The repro command is model-authored, built from a turn that read semi-
# trusted file content (the flagged file itself), and is delivered to a
# coding agent as something it will likely run verbatim in the real repo
# (hooks/logic.py's "[suggested repro ...]" text). safe_repro() is a coarse
# allowlist, not a sandbox: it only screens out the shapes that let one
# command smuggle a second (pipes, substitution, redirection, chaining) and
# restricts the first token to known-benign dev-tool invocations. Anything
# it rejects is simply never delivered — see verify_finding below.
REPRO_ALLOWED_PREFIXES = frozenset({
    "python3", "python", "pytest", "node", "npm", "go", "cargo", "make",
})
REPRO_UNSAFE_SUBSTRINGS = ("|", ";", "&", "$(", "`", ">", "<", "&&", "\n")


def safe_repro(cmd: str) -> bool:
    """True only when `cmd` starts with an allowlisted dev-tool token AND
    contains none of the shell metacharacters that could chain in a second
    command."""
    stripped = cmd.strip()
    if not stripped:
        return False
    first_token = stripped.split()[0]
    if first_token not in REPRO_ALLOWED_PREFIXES:
        return False
    return not any(bad in stripped for bad in REPRO_UNSAFE_SUBSTRINGS)


# Proof-by-exploit (Task 4): a plain SAST tool stops at "this pattern looks
# dangerous". When the judge's confirmed finding traces back to one of these
# mechanical screening signals (critic/screen.py's kind/cwe, linked by
# screen.match_signal), the verification turn is told to DEMONSTRATE the
# vulnerability class — not just re-read the code — before it may reply
# CONFIRMED. Keep each addendum short (<=6 lines); module-level so the CWE
# set is easy to extend alongside screen.py's _LINE_CHECKS.
EXPLOIT_ADDENDA: dict[str, str] = {
    "CWE-89": (
        "SECURITY CLASS: SQL injection (CWE-89). You must DEMONSTRATE the "
        "exploit — a repro that only re-reads the code is NOT verification.\n"
        "Craft an input that changes the query's STRUCTURE — e.g. `1 OR "
        "1=1` or a trailing `--` — and print the assembled query string so "
        "it visibly differs from the parameterized intent."
    ),
    "CWE-78": (
        "SECURITY CLASS: command injection (CWE-78). You must DEMONSTRATE "
        "the exploit — a repro that only re-reads the code is NOT "
        "verification.\nCraft an input with a shell metacharacter — e.g. "
        "`; touch /tmp/pwned` or `$(id)` — and show it runs as a SEPARATE "
        "command beyond the one the code intended."
    ),
    "CWE-95": (
        "SECURITY CLASS: code injection via eval/exec (CWE-95). You must "
        "DEMONSTRATE the exploit — a repro that only re-reads the code is "
        "NOT verification.\nCraft an input that executes attacker code "
        "through the eval/exec call — e.g. `__import__('os').system('id')` "
        "— and show it runs beyond the intended expression."
    ),
    "CWE-502": (
        "SECURITY CLASS: unsafe deserialization (CWE-502). You must "
        "DEMONSTRATE the exploit — a repro that only re-reads the code is "
        "NOT verification.\nCraft a malicious pickle/yaml/marshal payload "
        "for the deserializer and show it executes attacker-controlled "
        "behavior when loaded."
    ),
}


def build_prompt(suggestion: dict, staged_path: str, screen_signal: dict | None = None) -> str:
    loc = f"{suggestion['file']}:{suggestion['line']}" if suggestion.get("line") else suggestion["file"]
    text = (
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
    addendum = EXPLOIT_ADDENDA.get((screen_signal or {}).get("cwe", ""))
    if addendum:
        text += "\n\n" + addendum
    return text


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
        result = {"status": STATUSES[status], "note": redact(note.strip())[:300]}
    else:
        result = {"status": "inconclusive", "note": f"unparseable verify reply: {raw[:200]}"}
    # A repro is only ever meaningful (and only ever delivered) for a
    # confirmed finding — dropping it here for refuted/inconclusive replies
    # means no dead repro key ever lands in those rows.
    if result["status"] == "verified":
        repro_matches = _REPRO_RE.findall(stripped)
        if repro_matches:
            result["repro"] = _cap(redact(repro_matches[-1].strip()), REPRO_MAX_CHARS)
    return result


def verify_finding(repo: Path, suggestion: dict, system: str | None = None,
                    screen_signal: dict | None = None) -> dict:
    """Returns {"status": verified|refuted|inconclusive|error, "note": str}.

    screen_signal: the mechanical screening signal (critic/screen.py) linked
    to this finding by screen.match_signal, if any — when its cwe has a
    known exploit template (EXPLOIT_ADDENDA), the verification turn is asked
    to demonstrate the vulnerability class rather than just re-read the
    code. None (the default, and the outcome for any non-security finding)
    leaves the prompt byte-identical to before this parameter existed."""
    local = repo / suggestion.get("file", "")
    if not local.is_file():
        return {"status": "inconclusive", "note": "flagged file not found in repo"}
    # a throwaway staging dir: repro runs never touch the developer's repo, and
    # nothing is left behind for the critic to later flag as a finding
    staging = Path(tempfile.mkdtemp(prefix="codecouncil-verify-"))
    try:
        staged = staging / local.name
        shutil.copyfile(local, staged)
        reply = agent.ask(build_prompt(suggestion, str(staged), screen_signal), system=system,
                          tools=VERIFY_TOOLS, cwd=str(staging))
    except agent.AgentError as e:
        return {"status": "error", "note": str(e)[:200]}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result = parse(reply)
    if "repro" in result:
        result["repro"] = localize_repro(result["repro"], staging)
        # The repro is model-authored from semi-trusted file content and is
        # delivered to a coding agent as something worth running verbatim
        # (hooks/logic.py) — an unsafe shape (pipes, substitution, chaining)
        # is dropped here rather than shipped. The finding + note survive.
        if not safe_repro(result["repro"]):
            del result["repro"]
    return result
