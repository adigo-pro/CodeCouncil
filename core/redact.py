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
        # ghp_ (classic PAT) and github_pat_ (fine-grained) were the
        # original coverage; gh[ousr]_ adds GitHub's other first-party
        # token shapes -- oauth (gho_), user-to-server (ghu_),
        # server-to-server (ghs_), refresh (ghr_) -- all sharing the same
        # kind label since they're all GitHub-issued tokens.
        re.compile(
            r"(?:ghp_[A-Za-z0-9]{30,}"
            r"|gh[ousr]_[A-Za-z0-9]{36,}"
            r"|github_pat_[A-Za-z0-9_]{30,})"
        ),
    ),
    ("slack-token", re.compile(r"xox[bpoas]-[A-Za-z0-9-]{10,}")),
    ("groq-key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    (
        "stripe-key",
        re.compile(r"(?:sk_live_|rk_live_)[A-Za-z0-9]{20,}"),
    ),
    ("google-key", re.compile(r"AIza[A-Za-z0-9_-]{35}")),
    (
        "jwt",
        # A bare JWT (no assignment context -- e.g. inline in an
        # "Authorization: Bearer <token>" line) isn't caught by the
        # "assignment" pattern below, which requires a key/secret/token/
        # password-named variable immediately before it.
        re.compile(
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        ),
    ),
    (
        "assignment",
        re.compile(
            # The keyword only needs to appear *somewhere* in the name, so
            # SECRET_KEY / DB_PASSWORD / CLIENT_SECRET / JWT_SECRET /
            # AUTH_TOKEN — the common prefixed/compound env-var shapes — all
            # trigger redaction, not just a bare `key`/`secret`/... name.
            r"(?i)(?P<prefix>\b[A-Za-z0-9_-]*(?:key|secret|token|password|passwd)"
            r"[A-Za-z0-9_-]*\s*[=:]\s*['\"]?)"
            # The 16+ charset run is what qualifies the value as "high
            # entropy enough" to redact; once qualified, also consume any
            # trailing non-whitespace/non-quote characters (e.g. `!`, `%`)
            # so a special character after the clean run doesn't leave a
            # tail of the secret exposed past the marker.
            r"[A-Za-z0-9+/_=-]{" + str(ASSIGNMENT_MIN_VALUE_LEN) + r",}[^\s'\"]*",
        ),
    ),
    (
        "url-credential",
        # scheme://user:PASSWORD@host — a connection string embedding a
        # password in the URL itself (Postgres/Mongo/redis DSNs, git remote
        # URLs, curl one-liners). Only the password segment is redacted;
        # the scheme, username, and host are left visible, and the pattern
        # requires the literal ":...@" userinfo shape so a plain
        # "https://example.com/path" (no credentials) never matches. Run
        # last so an already-redacted key-shaped password (e.g. a
        # github-token used as a URL password) doesn't get relabeled: the
        # password class excludes "«" so a run that already begins with a
        # marker is left as-is rather than re-matched.
        re.compile(r"(?P<prefix>[a-z]+://[^:\s/]+:)[^@\s/«]+(?P<suffix>@)"),
    ),
]


# ANSI escape sequences (CSI "\x1b[...m", OSC "\x1b]...BEL", and bare
# two-character escapes) plus C0 control characters. Tab/newline/carriage
# return are deliberately preserved — they are ordinary content in a diff or
# a multi-line note.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI  — colour/cursor control
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC  — window title, hyperlinks
    r"|\x1b[@-Z\\-_]"                      # bare two-char escapes
)
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_controls(text: str) -> str:
    """Remove ANSI escapes and C0 control characters, keeping \\t/\\n/\\r.

    Model-authored strings (issue, rationale, verification note, repro) are
    printed to the developer's terminal by critic/render.py and injected into
    the coding agent's context by hooks/logic.py. Left raw, an escape sequence
    in that text can repaint the terminal — overwriting the severity label of
    the line above, or hiding text with a colour — so a finding could be made
    to *look* like something it is not. Cheap to remove at the boundary where
    model output is already being redacted and capped."""
    if not text:
        return text
    return _C0_RE.sub("", _ANSI_RE.sub("", text))


def sanitize(text: str) -> str:
    """The full boundary treatment for MODEL-AUTHORED text: strip terminal
    control sequences, then redact credential shapes.

    Strip runs first so an escape sequence spliced into the middle of a
    credential (`sk-abc\\x1b[0mdef…`) can't split it past the redaction
    patterns."""
    return redact(strip_controls(text))


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
