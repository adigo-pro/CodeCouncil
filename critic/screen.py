"""Mechanical screening of diffs for the documented top AI-code failure modes.

The research is blunt: ~45% of AI-generated code introduces OWASP-class
vulnerabilities while syntax correctness exceeds 95% (Veracode, 150+ models,
flat for two years); models hallucinate nonexistent packages often enough to
spawn a supply-chain attack class (slopsquatting); and agents under test
pressure weaken the tests themselves. None of these are visible as "broken
code" — which is exactly why a reviewer must look for them deliberately.

This module runs zero-model-cost static checks on the *added lines of a diff*
and produces signals the judgment prompt puts in front of the critic model:
confirm with a finding or dismiss with a reason. Signals are attention
direction, not verdicts — precision stays with the judge + verifier.

Pure except `resolve_new_imports` (spawns one python in the watched repo to
ask which imported top-level modules actually resolve there). Eval replays
pass no repo, so screening stays hermetic.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import deps

MAX_SIGNALS = 8
EVIDENCE_MAX_CHARS = 160
_RESOLVE_TIMEOUT = 20

_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py)$")
# shared by scan_test_weakening (per-signal) and test_integrity (per-session
# aggregate) — one definition of what counts as a test/assertion line.
_TEST_DEF_RE = re.compile(r"\s*def test_")
_ASSERT_RE = re.compile(r"\s*(assert\b|self\.assert)")
# string-BUILDING into a query: f-strings, .format, % interpolation, + concat.
# Deliberately NOT bare "%s" — a %s placeholder with a params argument is the
# safe parameterized form; flagging it would punish correct code.
_STR_BUILD_RE = re.compile(r'f["\']|%\s*\(|\.format\(|["\']\s*\+|\+\s*["\']|\(\s*\w+\s*\+')

# (kind, CWE tag, pattern over ONE added line); sql-injection additionally
# requires _STR_BUILD_RE on the same line
_LINE_CHECKS: list[tuple[str, str, re.Pattern[str]]] = [
    ("sql-injection", "CWE-89",
     re.compile(r'\.(execute|executemany|executescript)\s*\(')),
    ("command-injection", "CWE-78",
     re.compile(r'os\.system\s*\(\s*[^"\')]|shell\s*=\s*True')),
    ("unsafe-deserialization", "CWE-502",
     re.compile(r'pickle\.loads?\s*\(|yaml\.load\s*\((?![^)]*SafeLoader)[^)]*\)|marshal\.loads?\s*\(')),
    ("eval-injection", "CWE-95",
     re.compile(r'\b(eval|exec)\s*\(\s*[^"\')\s]')),
]

_IMPORT_RE = re.compile(r"^\s*(?:import\s+([A-Za-z_][\w]*)|from\s+([A-Za-z_][\w]*)[\w.]*\s+import\b)")


def added_lines_by_file(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Unified diff -> {path: [(approx_lineno, added_line), ...]}. Pure."""
    out: dict[str, list[tuple[int, str]]] = {}
    path, lineno = "", 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
            out.setdefault(path, [])
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) - 1 if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            lineno += 1
            if path:
                out[path].append((lineno, raw[1:]))
        elif not raw.startswith("-"):
            lineno += 1
    return out


def removed_lines_by_file(diff_text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    path = ""
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
        elif raw.startswith("-") and not raw.startswith("---") and path:
            out.setdefault(path, []).append(raw[1:])
    return out


def _evidence(line: str) -> str:
    line = line.strip()
    return line if len(line) <= EVIDENCE_MAX_CHARS else line[:EVIDENCE_MAX_CHARS] + "…"


def scan_patterns(diff_text: str) -> list[dict]:
    """Security-pattern signals over added lines. Pure."""
    signals = []
    for path, lines in added_lines_by_file(diff_text).items():
        for lineno, line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # no break: one line can carry two distinct classes
            # (os.system(eval(x)) is both command- and eval-injection)
            for kind, cwe, pattern in _LINE_CHECKS:
                if pattern.search(line):
                    if kind == "sql-injection" and not _STR_BUILD_RE.search(line):
                        continue
                    signals.append({"kind": kind, "cwe": cwe, "file": path,
                                    "line": lineno, "evidence": _evidence(line)})
    return signals


def scan_test_weakening(diff_text: str) -> list[dict]:
    """Removed tests/assertions in test files — the reward-hacking shape. Pure."""
    signals = []
    removed = removed_lines_by_file(diff_text)
    added = added_lines_by_file(diff_text)
    for path, lines in removed.items():
        if not _TEST_FILE_RE.search(path):
            continue
        gone_tests = [ln for ln in lines if _TEST_DEF_RE.match(ln)]
        gone_asserts = [ln for ln in lines if _ASSERT_RE.match(ln)]
        new_asserts = [ln for _n, ln in added.get(path, [])
                       if _ASSERT_RE.match(ln)]
        if gone_tests:
            signals.append({"kind": "test-removed", "cwe": "reward-hacking", "file": path,
                            "line": 0, "evidence": _evidence(gone_tests[0])})
        elif len(gone_asserts) > len(new_asserts):
            signals.append({"kind": "assertions-weakened", "cwe": "reward-hacking",
                            "file": path, "line": 0,
                            "evidence": f"{len(gone_asserts)} assertion(s) removed, "
                                        f"{len(new_asserts)} added"})
    return signals


_UNCHANGED_INTEGRITY = {"verdict": "unchanged", "tests_added": 0, "tests_removed": 0,
                        "asserts_added": 0, "asserts_removed": 0}


def test_integrity(diff_text: str) -> dict:
    """Session-level verdict: were this diff's tests strengthened, left
    unchanged, or weakened? Pure aggregation over the same added/removed
    line helpers and regexes scan_test_weakening uses, just summed instead
    of turned into per-file signals.

    weakened      = any removed `def test_` line, OR net-negative assertions
                     (assertions removed > assertions added), in a test file
    strengthened  = net-positive tests or assertions with nothing
                     removed-unreplaced (i.e. not weakened)
    unchanged     = everything else, including no test files touched at all
    """
    if not diff_text:
        return dict(_UNCHANGED_INTEGRITY)
    added = added_lines_by_file(diff_text)
    removed = removed_lines_by_file(diff_text)
    tests_added = tests_removed = asserts_added = asserts_removed = 0
    touched_test_file = False
    for path in set(added) | set(removed):
        if not _TEST_FILE_RE.search(path):
            continue
        touched_test_file = True
        added_lines = [ln for _n, ln in added.get(path, [])]
        removed_lines = removed.get(path, [])
        tests_added += sum(1 for ln in added_lines if _TEST_DEF_RE.match(ln))
        tests_removed += sum(1 for ln in removed_lines if _TEST_DEF_RE.match(ln))
        asserts_added += sum(1 for ln in added_lines if _ASSERT_RE.match(ln))
        asserts_removed += sum(1 for ln in removed_lines if _ASSERT_RE.match(ln))
    if not touched_test_file:
        return dict(_UNCHANGED_INTEGRITY)

    net_asserts = asserts_added - asserts_removed
    if tests_removed > 0 or net_asserts < 0:
        verdict = "weakened"
    elif tests_added > 0 or net_asserts > 0:
        verdict = "strengthened"
    else:
        verdict = "unchanged"
    return {"verdict": verdict, "tests_added": tests_added, "tests_removed": tests_removed,
            "asserts_added": asserts_added, "asserts_removed": asserts_removed}


def new_import_names(diff_text: str) -> dict[str, str]:
    """Top-level module names imported by ADDED python lines -> first file seen. Pure."""
    names: dict[str, str] = {}
    for path, lines in added_lines_by_file(diff_text).items():
        if not path.endswith(".py"):
            continue
        for _n, line in lines:
            m = _IMPORT_RE.match(line)
            if m:
                name = m.group(1) or m.group(2)
                names.setdefault(name, path)
    return names


def resolve_new_imports(names: dict[str, str], repo: Path) -> list[dict]:
    """Which imported top-level modules DON'T resolve in the repo's environment?
    The hallucinated-dependency (slopsquatting) check. One subprocess, stdlib only."""
    candidates = {n: f for n, f in names.items()
                  if n not in sys.stdlib_module_names}
    if not candidates:
        return []
    probe = ("import importlib.util,sys\n"
             "for n in sys.argv[1:]:\n"
             "    print(n, bool(importlib.util.find_spec(n)))\n")
    try:
        r = subprocess.run([sys.executable, "-c", probe, *candidates],
                           cwd=repo, capture_output=True, text=True,
                           timeout=_RESOLVE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []  # screening must never break judgment
    signals = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "False":
            name = parts[0]
            signals.append({"kind": "unresolvable-import", "cwe": "slopsquatting",
                            "file": candidates[name], "line": 0,
                            "evidence": f"import {name} — no such module resolves in "
                                        "this environment (hallucinated dependency?)"})
    return signals


def screen(diff_text: str, repo: Path | None = None) -> list[dict]:
    """All signals for a diff, capped. `repo=None` (eval replays) skips the
    import-resolution subprocess so replays stay hermetic; the typo-suspect
    check is pure (no subprocess) and runs either way.

    Typo-suspect runs BEFORE unresolvable-import, and a name flagged
    typo-suspect is excluded from the unresolvable check — a typo that
    happens to resolve (an attacker-registered slopsquat) is still suspect,
    but each imported name gets exactly one signal, not two.
    """
    if not diff_text:
        return []
    signals = scan_patterns(diff_text) + scan_test_weakening(diff_text)
    names = new_import_names(diff_text)
    signals += deps.suspicious_imports(names)
    if repo is not None:
        remaining = {n: f for n, f in names.items() if not deps.is_typo_suspect(n)}
        signals += resolve_new_imports(remaining, repo)
    return signals[:MAX_SIGNALS]


def match_signal(suggestion: dict, signals: list[dict]) -> dict | None:
    """Link a judge's SUGGESTION back to the screening signal that likely
    prompted it (Task 4: proof-by-exploit verification needs to know WHICH
    CWE class to demonstrate). Pure.

    Deliberately simple matching rule: a signal is a candidate when it
    shares the suggestion's file basename AND either (a) the signal's kind
    (e.g. "sql-injection" -> "sql injection") is named in the model's issue
    text, or (b) the signal's line is within 3 of the suggestion's line (a
    signal with no line, i.e. line 0, never proximity-matches). If more than
    one DISTINCT (kind, cwe) pair ends up a candidate, the link is
    ambiguous and nothing is attached — a wrong exploit class in the
    verification prompt is worse than no addendum at all.
    """
    sugg_base = Path(suggestion.get("file") or "").name
    if not sugg_base:
        return None
    sugg_line = suggestion.get("line")
    issue = (suggestion.get("issue") or "").lower()
    candidates: set[tuple[str, str]] = set()
    for s in signals:
        if Path(s.get("file") or "").name != sugg_base:
            continue
        kind = s.get("kind", "")
        kind_named = bool(kind) and kind.replace("-", " ") in issue
        sig_line = s.get("line") or 0
        near_line = (isinstance(sugg_line, int) and sig_line > 0
                     and abs(sig_line - sugg_line) <= 3)
        if kind_named or near_line:
            candidates.add((kind, s.get("cwe", "")))
    if len(candidates) != 1:
        return None
    kind, cwe = next(iter(candidates))
    return {"kind": kind, "cwe": cwe}


def render(signals: list[dict]) -> list[str]:
    """Prompt lines for a signal list. Pure."""
    if not signals:
        return []
    lines = ["MECHANICAL SCREENING SIGNALS (static checks for documented AI-code "
             "failure modes — each is attention direction, not a verdict; confirm "
             "the genuine ones with a finding, dismiss the rest with a reason):"]
    for s in signals:
        loc = f"{s['file']}:{s['line']}" if s.get("line") else s["file"]
        lines.append(f"- [{s['kind']} · {s['cwe']}] {loc} — {s['evidence']}")
    lines.append("")
    return lines
