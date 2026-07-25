"""Dependency provenance: typo-suspect imports and new-dependency lines.

Two mechanical, offline checks for the "resolvable but wrong" hallucination
shape (slopsquatting): a model writes `import requsts` instead of
`import requests`. That is a DIFFERENT failure than an import that doesn't
resolve at all (screen.resolve_new_imports) — an attacker-registered
typo-squat package resolves just fine, so "does it import cleanly" isn't
the same question as "is this the package we meant". Pure, no network, no
filesystem — matches critic/screen.py's existing pure/impure split.
"""

from __future__ import annotations

import re
import sys

from .pkg_names import PKG_NAMES

_KNOWN = frozenset(PKG_NAMES)

_REQUIREMENTS_RE = re.compile(r"^requirements.*\.txt$")
# pyproject.toml dependency-array entries: a quoted requirement string,
# optionally with a trailing comma — e.g. `"requests>=2.0",` or `"flask"`.
# Deliberately simple (no TOML parser, no section-awareness) per the brief:
# this will also match a stray quoted string outside a dependency array, but
# added lines in a diff hunk touching pyproject.toml are overwhelmingly
# dependency entries, and a receipt over-listing a non-dependency quoted
# string is a much cheaper mistake than missing a real one.
_PYPROJECT_DEP_RE = re.compile(r'^"[A-Za-z0-9_.\-]+(\s*[<>=!~^].*)?",?$')
# package.json dependency-object entries: `"name": "version-range",` — this
# also matches ordinary JSON string properties elsewhere in the file (same
# simple-filter tradeoff as pyproject.toml above).
_PACKAGE_JSON_DEP_RE = re.compile(r'^"[A-Za-z0-9_.@/\-]+"\s*:\s*"[^"]*"\s*,?$')


def _within_one_edit(a: str, b: str) -> bool:
    """True if the Damerau-Levenshtein distance between a and b is 0 or 1.

    Decided directly, without a DP table, since distance <=1 has a closed
    form by length:
      - equal length: exactly one substitution, or one adjacent-character
        transposition (both count as a single Damerau-Levenshtein edit)
      - length differs by 1: a single insertion/deletion — checked with the
        standard two-pointer "is one a subsequence of the other with at
        most one skip" scan
      - length differs by >=2: impossible in one edit
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diffs = [i for i in range(la) if a[i] != b[i]]
        if len(diffs) == 1:
            return True  # single substitution
        if len(diffs) == 2:
            i, j = diffs
            return j == i + 1 and a[i] == b[j] and a[j] == b[i]  # adjacent transposition
        return False
    shorter, longer = (a, b) if la < lb else (b, a)
    i = j = skipped = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = 1
        j += 1  # skip one char in the longer string
    return True


def _nearest_known(name: str) -> str | None:
    # Iterate sorted to ensure deterministic evidence for reproducible receipts/prompts
    for known in sorted(_KNOWN):
        if _within_one_edit(name, known):
            return known
    return None


def is_typo_suspect(name: str) -> bool:
    """True if `name` is not itself a known package but is exactly one edit
    from one. Shared by suspicious_imports (signal text) and
    screen.screen (excluding a typo-suspect name from the separate
    unresolvable-import check — one signal per name, per the brief)."""
    return name not in _KNOWN and _nearest_known(name) is not None


def suspicious_imports(names: dict[str, str]) -> list[dict]:
    """For each imported top-level name that is NOT a known package and NOT
    stdlib: if it's within edit-distance 1 of a known package name, emit a
    typo-suspect-import signal naming the near-miss. A name exactly IN the
    known list emits nothing — it's legitimate. `names` is
    screen.new_import_names' output (name -> first file it was imported in).
    """
    signals = []
    for name, path in names.items():
        if name in _KNOWN or name in sys.stdlib_module_names:
            continue
        # Don't flag as typo of external package if it's a typo of stdlib
        if any(_within_one_edit(name, stdlib) for stdlib in sys.stdlib_module_names):
            continue
        known = _nearest_known(name)
        if known:
            signals.append({
                "kind": "typo-suspect-import", "cwe": "slopsquatting",
                "file": path, "line": 0,
                "evidence": f"import {name} — 1 edit from '{known}'",
            })
    return signals


def new_dependency_lines(diff_text: str) -> list[str]:
    """Added lines in requirements*.txt / pyproject.toml / package.json —
    the receipt's claims-vs-verified supply-chain section: what dependency
    lines did this session's diffs actually add?

    requirements*.txt: every non-blank, non-comment added line IS a
    dependency entry (that's the whole file format).
    pyproject.toml / package.json: no real parser here (would need one to
    be section-aware) — a per-line shape filter that matches typical
    dependency-array/object entries and lets structural lines (section
    headers, brackets, braces) fall through uncounted.
    """
    from . import screen  # local import: screen imports this module too

    out: list[str] = []
    for path, lines in screen.added_lines_by_file(diff_text).items():
        base = path.rsplit("/", 1)[-1]
        stripped = [ln.strip() for _n, ln in lines]
        if _REQUIREMENTS_RE.match(base):
            out.extend(ln for ln in stripped if ln and not ln.startswith("#"))
        elif base == "pyproject.toml":
            out.extend(ln for ln in stripped if _PYPROJECT_DEP_RE.match(ln))
        elif base == "package.json":
            out.extend(ln for ln in stripped if _PACKAGE_JSON_DEP_RE.match(ln))
    return out
