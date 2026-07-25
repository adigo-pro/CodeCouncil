"""Prove findings before delivering them: the critic runs a repro before speaking.

A suggestion that arrives with 'VERIFIED: called safe_divide(1, 0), got
ZeroDivisionError' is a different product from a plausible guess — and a
REFUTED finding never reaches the developer at all.

The flagged file is staged into a throwaway directory and a NO-TOOLS pi turn
is asked to write ONE self-contained Python script that reproduces the
claimed issue (or demonstrates it's false). The harness -- not the model --
then executes that script for real against the staged file. This mirrors
critic/probe.py's approach (script + shared execution primitive), and
replaces an earlier tool-enabled turn (read+bash) that asked the model to
run its own repro and report back a status line: the NVIDIA/pi backend
frequently emitted those tool calls as literal, never-executed text, which
made verification land "inconclusive" and withheld true findings. A script
the harness executes itself has no such failure mode.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.redact import redact

from . import agent
from .probe import run_script, strip_fence

VERIFY_EXEC_TIMEOUT = 60  # seconds -- a hanging verification script must never wedge a beat

# The repro delivered to a coding agent is now the executed script itself
# (previously a single shell command) -- long enough to need the same
# "… [N chars total]" capping discipline as every other prompt/finding sink
# in this repo (observer/transcript.py, observer/gitwatch.py, critic/prompt.py).
REPRO_MAX_CHARS = 2000

# Exactly one marker line, matched anywhere in the executed script's stdout.
# "First marker wins" for the note text (re.search finds the first match);
# BOTH markers present is treated as a contradictory script, not a verdict.
_CONFIRMED_RE = re.compile(r"^CONFIRMED:\s*(.+)$", re.MULTILINE)
_REFUTED_RE = re.compile(r"^REFUTED:\s*(.+)$", re.MULTILINE)


# Proof-by-exploit (Task 4): a plain SAST tool stops at "this pattern looks
# dangerous". When the judge's confirmed finding traces back to one of these
# mechanical screening signals (critic/screen.py's kind/cwe, linked by
# screen.match_signal), the verification turn is told to DEMONSTRATE the
# vulnerability class in its script -- not just re-read the code -- before
# it may print CONFIRMED. Keep each addendum short (<=6 lines); module-level
# so the CWE set is easy to extend alongside screen.py's _LINE_CHECKS.
EXPLOIT_ADDENDA: dict[str, str] = {
    "CWE-89": (
        "SECURITY CLASS: SQL injection (CWE-89). Your script must "
        "DEMONSTRATE the exploit — a script that only re-reads the code is "
        "NOT verification.\n"
        "Craft an input that changes the query's STRUCTURE — e.g. `1 OR "
        "1=1` or a trailing `--` — and print the assembled query string so "
        "it visibly differs from the parameterized intent."
    ),
    "CWE-78": (
        "SECURITY CLASS: command injection (CWE-78). Your script must "
        "DEMONSTRATE the exploit — a script that only re-reads the code is "
        "NOT verification.\nCraft an input with a shell metacharacter — "
        "e.g. `; touch /tmp/pwned` or `$(id)` — and show it runs as a "
        "SEPARATE command beyond the one the code intended."
    ),
    "CWE-95": (
        "SECURITY CLASS: code injection via eval/exec (CWE-95). Your "
        "script must DEMONSTRATE the exploit — a script that only re-reads "
        "the code is NOT verification.\nCraft an input that executes "
        "attacker code through the eval/exec call — e.g. "
        "`__import__('os').system('id')` — and show it runs beyond the "
        "intended expression."
    ),
    "CWE-502": (
        "SECURITY CLASS: unsafe deserialization (CWE-502). Your script "
        "must DEMONSTRATE the exploit — a script that only re-reads the "
        "code is NOT verification.\nCraft a malicious pickle/yaml/marshal "
        "payload for the deserializer and show it executes "
        "attacker-controlled behavior when loaded."
    ),
}


def build_prompt(suggestion: dict, staged_path: str, screen_signal: dict | None = None) -> str:
    loc = f"{suggestion['file']}:{suggestion['line']}" if suggestion.get("line") else suggestion["file"]
    text = (
        "TASK: VERIFY\n\n"
        f"FINDING: [{suggestion['severity'].upper()}] {loc} — {suggestion['issue']}\n"
        f"Rationale: {suggestion.get('rationale', '')}\n\n"
        f"The file under review is at: {staged_path}\n\n"
        "Write ONE self-contained Python script that tests this finding "
        "against that file. Reply with ONLY the script -- no explanation, "
        "no status line, just code (a single ```python fenced block or "
        "bare source, either is fine). Do not use any tool -- the script "
        "will be executed FOR you, with the staged file present and the "
        "script's own directory as its working directory.\n\n"
        "The script must print exactly one final line AND exit with code 0:\n"
        "CONFIRMED: <one-line evidence> -- if running it reproduces the "
        "claimed problem (the bad behavior actually happens)\n"
        "REFUTED: <one-line evidence> -- if running it demonstrates the "
        "finding is wrong (the code behaves correctly)\n"
        "If the finding cannot be tested this way, print neither line and exit 0."
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
    — a script that embeds the absolute path it was told about should read
    naturally once shown to whoever's looking at the actual repo."""
    return repro.replace(str(staging), ".")


def _classify(stdout: str, stderr: str, returncode: int = 0) -> dict:
    """Classify one executed verification script's captured output. A
    CONFIRMED/REFUTED line is the script's OWN verdict on itself (it ran for
    real, against the staged file) -- anything else (crash, no verdict line,
    both lines at once) can never become a verified/refuted finding; a
    broken or contradictory script proves nothing.

    Both the marker AND a clean exit (returncode == 0) are required for a
    verdict. A script that prints the marker but exits nonzero is inconclusive."""
    confirmed = _CONFIRMED_RE.search(stdout)
    refuted = _REFUTED_RE.search(stdout)
    if confirmed and refuted:
        return {"status": "inconclusive",
                "note": "verification script printed both CONFIRMED and REFUTED"}
    if confirmed:
        if returncode != 0:
            return {"status": "inconclusive",
                    "note": f"CONFIRMED printed but script exited {returncode} — untrusted"}
        return {"status": "verified", "note": redact(confirmed.group(1).strip())[:300]}
    if refuted:
        if returncode != 0:
            return {"status": "inconclusive",
                    "note": f"REFUTED printed but script exited {returncode} — untrusted"}
        return {"status": "refuted", "note": redact(refuted.group(1).strip())[:300]}
    diag = (stderr or stdout).strip()
    note = "verification script printed no CONFIRMED/REFUTED marker"
    if diag:
        # a traceback's most useful line (the exception itself) is its
        # LAST line, so truncate from the front when it's long rather than
        # the back -- the opposite of this codebase's usual head-cap.
        note += f": {diag if len(diag) <= 200 else '…' + diag[-200:]}"
    return {"status": "inconclusive", "note": note}


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
        try:
            reply = agent.ask(build_prompt(suggestion, str(staged), screen_signal), system=system)
        except agent.AgentError as e:
            return {"status": "error", "note": str(e)[:200]}
        script = strip_fence(reply.strip())
        if not script:
            return {"status": "inconclusive", "note": "empty verify reply"}
        try:
            res = run_script(staging, script, VERIFY_EXEC_TIMEOUT, filename="verify_script.py")
        except subprocess.TimeoutExpired:
            return {"status": "inconclusive",
                    "note": f"verification script timed out after {VERIFY_EXEC_TIMEOUT}s"}
        except OSError as e:
            return {"status": "inconclusive",
                    "note": f"verification script failed to run: {str(e)[:150]}"}
        result = _classify(res.stdout or "", res.stderr or "", res.returncode)
        if result["status"] == "verified":
            result["repro"] = _cap(redact(localize_repro(script, staging)), REPRO_MAX_CHARS)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
