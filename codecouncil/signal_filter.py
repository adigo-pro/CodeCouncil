"""Signal-vs-noise classification for the launcher's live terminal.

The daemons stay fully verbose (running one solo is a debugging view); the
LAUNCHER decides what a person watching the council actually needs to see.
Three classes:

  highlight — the moments that prove usefulness: findings, verified proofs,
              grades, heuristics rewrites/rollbacks, receipts, council votes.
  normal    — the live narration (observer feed, commits, PASS verdicts).
  drop      — idle-beat chatter that says "nothing happened" (unmuted by
              /verbose; a periodic heartbeat line survives so the terminal
              never looks dead).

Pure functions — no I/O — so the classification is trivially testable.
"""

from __future__ import annotations

import re
import threading

# toggled by the console's /verbose; read by the launcher's pump
VERBOSE = threading.Event()

# idle chatter: critic's nothing-new/held/cooling/in-flight beats and the
# reflector's empty passes. ANSI codes may wrap the text — strip first.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DROP_RE = re.compile(
    r"^(· beat \d+ · [\d:]+ · (nothing new|.*held — no code change|cooling down|turn in flight)"
    r"|reflector: nothing to grade$)"
)
_HIGHLIGHT_RE = re.compile(
    r"^(■ beat "                       # a finding (severity line)
    r"|  verify (verified|refuted)"    # repro proof outcome
    r"|⚠ beat .*malformed"             # format-drift warning
    r"|reflector: \w+ → (accepted|rebutted|ignored|missed|undelivered)"
    r"|reflector: \w+ → distilled fact"
    r"|reflector: heuristics v\d+ → v\d+"
    r"|reflector: rewrite (rejected|rolled back)"
    r"|critic: receipt written"
    r"|critic: council mode"
    r"|  council: )"
)

DROP = "drop"
NORMAL = "normal"
HIGHLIGHT = "highlight"


def classify(line: str) -> str:
    text = _ANSI_RE.sub("", line).rstrip()
    if _HIGHLIGHT_RE.match(text):
        return HIGHLIGHT
    if _DROP_RE.match(text):
        return DROP
    return NORMAL
