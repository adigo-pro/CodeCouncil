"""Redact high-confidence credential shapes before text leaves the machine.

Applied at capture time in observer/gitwatch.py so nothing downstream
(prompts, dashboard, evidence) ever holds a real secret value. Deliberately
conservative: only shapes that are near-unambiguously a credential get
replaced, because a false redaction of ordinary code is worse than a miss.
The marker format is exact and stable — `critic/persona.md` and
`critic/heuristics.seed.md` teach the model that seeing this marker IS a
confirmed secret-in-code finding, without ever seeing the value.
"""

from __future__ import annotations

import re

# Minimum length of an assignment's value before it's "high entropy enough"
# to redact. Anything shorter is left alone — too easy to be a placeholder
# like "changeme" or a short test fixture.
ASSIGNMENT_MIN_VALUE_LEN = 16

PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "private-key",
        re.compile(
            r"(?P<prefix>-----BEGIN [A-Z ]*PRIVATE KEY-----\r?\n)"
            r".*?"
            r"(?P<suffix>\r?\n-----END [A-Z ]*PRIVATE KEY-----)",
            re.DOTALL,
        ),
    ),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("nvidia-key", re.compile(r"nvapi-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    (
        "github-token",
        re.compile(r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    ),
    ("slack-token", re.compile(r"xox[bpoas]-[A-Za-z0-9-]{10,}")),
    (
        "assignment",
        re.compile(
            # The keyword only needs to appear *somewhere* in the name, so
            # SECRET_KEY / DB_PASSWORD / CLIENT_SECRET / JWT_SECRET /
            # AUTH_TOKEN — the common prefixed/compound env-var shapes — all
            # trigger redaction, not just a bare `key`/`secret`/... name.
            r"(?i)(?P<prefix>\b[A-Za-z0-9_-]*(?:key|secret|token|password|passwd)"
            r"[A-Za-z0-9_-]*\s*[=:]\s*['\"]?)"
            r"[A-Za-z0-9+/_=-]{" + str(ASSIGNMENT_MIN_VALUE_LEN) + r",}",
        ),
    ),
]


def redact(text: str) -> str:
    """Replace every high-confidence credential shape in `text` with a
    `«REDACTED:kind»` marker. Non-secret text (including the surrounding
    variable name for assignments, and the BEGIN/END lines for PEM blocks)
    is left untouched.
    """
    if not text:
        return text
    for kind, pattern in PATTERNS:
        marker = f"«REDACTED:{kind}»"

        def repl(m: re.Match, marker: str = marker) -> str:
            groups = m.groupdict()
            return f"{groups.get('prefix', '')}{marker}{groups.get('suffix', '')}"

        text = pattern.sub(repl, text)
    return text
