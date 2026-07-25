"""Property probes: the almost-right detector.

Generalizes the docstring-trap catch. A function that carries a PROMISE (its
docstring) is a testable contract even when nothing else about the diff looks
wrong. For each such changed function, one budgeted model turn derives up to
`MAX_PROBES_PER_FUNC` short, self-checking Python scripts that probe an edge
case implied by the promise; each runs for real in a throwaway staging copy
of the file (the same "prove it before speaking" discipline as
`critic/verify.py`, but the proof here is generated up front rather than
reconstructed after a suggestion already exists). The first probe whose
execution demonstrates the code CONTRADICTING its own docstring becomes a
finding whose repro is the probe itself.

`candidates()` is diff-only and deliberately conservative: it reconstructs an
added function's source purely from the diff's `+` lines (no filesystem
access, no post-diff file content), by design (module boundary: this is the
pure half of the pass, testable without a repo on disk). A block of added
lines that doesn't parse as a complete function on its own -- most commonly a
partial edit to an EXISTING function, where only some body lines changed and
the rest is unchanged context the diff never shows as "+" -- is silently
skipped rather than guessed at. One consequence worth knowing: a docstring
promise added to a *method* (as opposed to a top-level function) loses its
class prefix in `qualname` (falls back to just the method name) because the
diff block for a newly-added method never includes its enclosing `class`
line -- the reconstruction dedents the block to make it parse, which is
enough to find the function but not to recover its class. Precision first:
better an occasional missed candidate or a slightly-short qualname than a
guessed-at function body used to author a probe against code that isn't
actually there.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Callable

MAX_PROBES_PER_FUNC = 3
MAX_PROBE_CALLS_PER_BEAT = 2  # TASK: PROBE model turns allowed per beat
PROBE_TIMEOUT = 20  # seconds -- a hanging probe must never wedge a beat
# Every other prompt sink in this repo caps inlined file/diff content with a
# module constant and a "… [N chars total]" marker (observer/transcript.py,
# observer/gitwatch.py, critic/prompt.py's _cap) -- build_prompt inlines the
# ENTIRE current file, so it needs the same discipline or a large file blows
# the prompt budget unbounded.
PROBE_SOURCE_MAX_CHARS = 6000

_FILE_HEADER_RE = re.compile(r"^\+\+\+ b/(.+)$")
_PROBE_MARKER_RE = re.compile(r"^PROBE:\s*$", re.MULTILINE)
_DIVERGES_RE = re.compile(r"^DIVERGES:\s*(.+)$", re.MULTILINE)
_CONSISTENT_RE = re.compile(r"^CONSISTENT:\s*(.+)$", re.MULTILINE)


def _added_blocks(diff_text: str) -> list[tuple[str, str]]:
    """(file, block) pairs, one per maximal run of consecutive '+' lines in
    the diff (the '+' stripped). A run ends at any context/'-' line, a new
    hunk header, or a new file header -- so a block is always literally
    contiguous newly-added text, never stitched across a gap."""
    blocks: list[tuple[str, str]] = []
    current_file: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf and current_file:
            blocks.append((current_file, "\n".join(buf)))
        buf = []

    for line in diff_text.splitlines():
        m = _FILE_HEADER_RE.match(line)
        if m:
            flush()
            current_file = m.group(1)
            continue
        if line.startswith("---") or line.startswith("+++"):
            flush()
            continue
        if line.startswith("+"):
            buf.append(line[1:])
        else:
            flush()
    flush()
    return blocks


def _parse_block(text: str) -> tuple[ast.AST, str] | None:
    """Try to parse an added-lines block as Python: as-is first (a fully
    added top-level function keeps its real indentation, so qualname/class
    nesting stays accurate), then dedented (a fully added method/nested
    function, whose added lines carry their original indentation but no
    longer have the enclosing class/def line to make that indentation valid
    on its own). Neither parsing -> None: this block cannot be cleanly
    reconstructed from the diff alone (most likely a partial edit to an
    existing function), so it is skipped rather than guessed at."""
    for candidate_text in (text, textwrap.dedent(text)):
        try:
            tree = ast.parse(candidate_text)
        except SyntaxError:
            continue
        return tree, candidate_text
    return None


def _walk_functions(tree: ast.AST, prefix: str = "") -> list[tuple[str, str]]:
    """(qualname, docstring) for every function/method under `tree` that
    carries a docstring. Recurses into classes (building "Class.method").
    Also recurses into function bodies to catch a nested def with its own
    docstring, but does NOT extend the prefix for it -- a function nested
    inside "Foo.outer" is reported as "Foo.inner", never "Foo.outer.inner".
    Best-effort, not a fully qualified name."""
    out: list[tuple[str, str]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            out.extend(_walk_functions(node, f"{prefix}{node.name}."))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc:
                out.append((f"{prefix}{node.name}", doc.strip()))
            out.extend(_walk_functions(node, prefix))
    return out


def candidates(diff_text: str) -> list[dict]:
    """PURE: added `def`/`async def` blocks (fully new code per the diff's
    '+' lines) that carry a docstring. See the module docstring for the
    diff-only reconstruction and its precision-first limitation. Returns
    [{"file", "qualname", "promise"}, ...], deduped by (file, qualname).
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for file, block in _added_blocks(diff_text):
        if not file.endswith(".py"):
            continue
        parsed = _parse_block(block)
        if parsed is None:
            continue
        tree, _source = parsed
        for qualname, doc in _walk_functions(tree):
            key = (file, qualname)
            if key in seen:
                continue
            seen.add(key)
            out.append({"file": file, "qualname": qualname, "promise": doc})
    return out


def build_prompt(candidate: dict, source: str, module_name: str) -> str:
    if len(source) > PROBE_SOURCE_MAX_CHARS:
        source = source[:PROBE_SOURCE_MAX_CHARS] + f"\n… [{len(source)} chars total]"
    return (
        "TASK: PROBE\n\n"
        f"FUNCTION: {candidate['qualname']} in {candidate['file']}\n"
        f"PROMISE (its docstring): {candidate['promise']}\n\n"
        "CURRENT FILE CONTENTS:\n" + source + "\n\n"
        f"The file is importable as module `{module_name}` "
        f"(e.g. `from {module_name} import ...`) from your working directory.\n\n"
        "Derive up to 3 short, independent Python scripts. Each script must "
        "probe ONE edge case implied by the promise above (an invalid input, "
        "a boundary value, the specific behavior the docstring claims) and:\n"
        "  - import the module and call the function directly -- no test "
        "framework\n"
        "  - wrap the call in try/except so the script itself decides "
        "whether what happened CONTRADICTS the promise\n"
        "  - print EXACTLY one final line:\n"
        "    DIVERGES: <what actually happened, vs. what was promised> -- "
        "if it contradicts the promise\n"
        "    CONSISTENT: <short note> -- if behavior matches the promise\n\n"
        "Reply with each script preceded by its own line reading exactly "
        "\"PROBE:\" and nothing else on that line -- no prose, no markdown "
        "fences, no numbering outside that marker."
    )


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*)\n```\s*$", re.DOTALL)


def strip_fence(script: str) -> str:
    """Observed live: despite the prompt explicitly saying "no markdown
    fences", the model still wraps probes in ```python ... ``` often enough
    to matter -- a fenced script is a SyntaxError as a .py file, which would
    misclassify a genuine divergence as a broken probe (the opposite of
    precision-first: it would SILENCE a real catch, not manufacture a false
    one). Defensive, not load-bearing for anything else: a script with no
    fence passes through unchanged.

    Shared with critic/verify.py's script-based verification -- one fence
    convention, one place that strips it."""
    m = _FENCE_RE.match(script.strip())
    return m.group(1) if m else script


def _parse_probes(raw: str) -> list[str]:
    """Split a TASK: PROBE reply on "PROBE:" marker lines. Malformed replies
    (no marker at all, or only empty bodies) yield an empty list -- callers
    treat that as "no finding", never as an error."""
    parts = _PROBE_MARKER_RE.split(raw)
    scripts = [strip_fence(p.strip("\n").strip()) for p in parts[1:]]
    return [s for s in scripts if s][:MAX_PROBES_PER_FUNC]


def run_script(staging: Path, script_src: str, timeout: int,
               filename: str = "script.py") -> subprocess.CompletedProcess:
    """Write `script_src` to `filename` in `staging` and execute it for
    real: sys.executable, cwd=staging, PYTHONPATH=staging (so `import
    <module>` finds whatever was staged alongside it), capturing
    stdout/stderr as text. The one execution primitive shared by probe.py's
    probe scripts and verify.py's script-based verification (Task:
    script-verification -- the model writes a script, the harness executes
    it instead of relying on tool calls the pi/NVIDIA backend sometimes
    emits as inert text).

    The script is model-authored -- untrusted -- so its environment is a
    MINIMAL ALLOWLIST built from scratch, never `{**os.environ, ...}`: no
    API keys, no cloud creds, nothing sensitive reaches the child. HOME is
    redirected into `staging`, so `os.path.expanduser("~/.codecouncil/env")`
    and `~/.ssh` resolve INSIDE staging (nonexistent) rather than the real
    home -- a large risk reduction with zero dependencies. This is a
    credential-exposure mitigation, not a full sandbox: a malicious script
    can still read absolute filesystem paths or make network calls (see
    SECURITY.md's trust-boundary note); a full OS sandbox is roadmap.

    Uses sys.executable rather than a hardcoded "python3" so a venv/pyenv
    interpreter mismatch can't make staged imports fail spuriously -- with
    the scrubbed env still passing through the real PATH, sys.executable
    is an absolute path anyway, so this works regardless.

    Raises subprocess.TimeoutExpired / OSError -- callers decide how to
    classify that; neither probe nor verify treats a crashed harness as a
    finding."""
    script_path = staging / filename
    script_path.write_text(script_src, encoding="utf-8")
    env = {
        "PYTHONPATH": str(staging),
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(staging),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
    }
    env = {k: v for k, v in env.items() if v}
    return subprocess.run(
        [sys.executable, str(script_path)], capture_output=True, text=True,
        timeout=timeout, cwd=str(staging), env=env)


def _execute_probe(staging: Path, probe_src: str) -> dict:
    """Run one probe script for real in the staging dir and classify what
    happened. A DIVERGES/CONSISTENT line is the script's own verdict on
    itself; anything else (crash, timeout, no verdict line at all) is
    "error" -- a broken probe proves nothing about the code under test."""
    try:
        res = run_script(staging, probe_src, PROBE_TIMEOUT, filename="probe_script.py")
    except subprocess.TimeoutExpired:
        return {"status": "error", "note": "probe timed out"}
    except OSError as e:
        return {"status": "error", "note": str(e)[:200]}
    stdout = res.stdout or ""
    diverges = _DIVERGES_RE.findall(stdout)
    if diverges:
        return {"status": "diverges", "note": diverges[-1].strip()}
    consistent = _CONSISTENT_RE.findall(stdout)
    if consistent:
        return {"status": "consistent", "note": consistent[-1].strip()}
    return {"status": "error", "note": (res.stderr or stdout).strip()[:200]}


def _line_for(path: Path, qualname: str) -> int | None:
    """Best-effort line number of `qualname`'s def in the CURRENT file
    content (not the diff) -- location metadata only, never load-bearing
    for whether a finding is delivered."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return None
    target = qualname.split(".")[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target:
            return node.lineno
    return None


def run_probes(candidate: dict, repo: Path, ask: Callable[[str], str]) -> dict | None:
    """One TASK: PROBE model turn for `candidate`, then execute each
    returned probe (up to MAX_PROBES_PER_FUNC) for real against a staging
    copy of the current file. Returns the first finding whose probe
    demonstrates a contradiction, or None -- including when every probe was
    itself broken (errored) rather than diverging: a broken probe is never
    evidence of a code problem.
    """
    local = Path(repo) / candidate.get("file", "")
    if not local.is_file():
        return None
    try:
        source = local.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    module_name = local.stem
    try:
        reply = ask(build_prompt(candidate, source, module_name))
    except Exception:
        return None
    probe_scripts = _parse_probes(reply)
    if not probe_scripts:
        return None
    staging = Path(tempfile.mkdtemp(prefix="codecouncil-probe-"))
    try:
        shutil.copyfile(local, staging / local.name)
        for probe_src in probe_scripts:
            result = _execute_probe(staging, probe_src)
            if result["status"] == "diverges":
                promise = candidate.get("promise", "")[:150]
                return {
                    "file": candidate["file"],
                    "line": _line_for(local, candidate.get("qualname", "")),
                    "issue": f"docstring promises {promise!r}; "
                             f"probe shows {result['note'][:150]}",
                    "repro": probe_src.strip(),
                }
            # "consistent" -> the probe found no contradiction, try the next
            # one. "error" -> the probe itself was broken (bad import, typo,
            # crash unrelated to the promise) -- also try the next one, but
            # this specific probe can never become a finding.
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return None
