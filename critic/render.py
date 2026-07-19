"""Terminal view: loud suggestions, quiet PASSes."""

from __future__ import annotations

import sys

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def render_verdict(beat: int, ts: str, verdict: dict) -> None:
    clock = ts.split("T")[1][:8] if "T" in ts else ts
    if verdict["verdict"] == "PASS":
        note = " (malformed reply)" if "malformed" in verdict else ""
        reason = f" — {verdict['reason']}" if verdict.get("reason") else ""
        print(_c("2", f"✓ beat {beat} · {clock} · PASS{reason}{note}"))
        return
    s = verdict["suggestion"]
    sev_color = {"high": "31", "medium": "33", "low": "36"}.get(s["severity"], "33")
    loc = f"{s['file']}:{s['line']}" if s.get("line") else s["file"]
    kind = " · task review" if verdict.get("review_kind") == "task" else ""
    print(_c("1;" + sev_color, f"■ beat {beat} · {clock} · {s['severity'].upper()} · {loc}{kind}"))
    print(f"  {s['issue']}")
    if s.get("rationale"):
        print(_c("2", f"  why: {s['rationale']}"))
    v = verdict.get("verification")
    if v:
        mark = {"verified": "32", "refuted": "31"}.get(v["status"], "2")
        print(_c(mark, f"  sandbox {v['status']}: {v['note']}"))


def render_quiet(beat: int, ts: str) -> None:
    clock = ts.split("T")[1][:8] if "T" in ts else ts
    print(_c("2", f"· beat {beat} · {clock} · nothing new, no call made"))


def render_status(beat: int, ts: str, msg: str) -> None:
    clock = ts.split("T")[1][:8] if "T" in ts else ts
    print(_c("2", f"· beat {beat} · {clock} · {msg}"))


def render_error(beat: int, ts: str, msg: str) -> None:
    clock = ts.split("T")[1][:8] if "T" in ts else ts
    print(_c("31", f"! beat {beat} · {clock} · agent error: {msg}"))
