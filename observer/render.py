"""Live terminal view — the human-facing half of each heartbeat."""

from __future__ import annotations

import sys

from .events import COMMIT, DIFF, REASONING, TOOL_CALL, Event

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def render_beat(beat: int, ts: str, events: list[Event]) -> None:
    clock = ts.split("T")[1][:8] if "T" in ts else ts
    if not events:
        print(_c("2", f"♥ beat {beat} · {clock} · quiet"))
        return
    print(_c("1", f"♥ beat {beat} · {clock} · {len(events)} event(s)"))
    for e in events:
        sid = (e.session or "")[:8]
        if e.type == REASONING:
            print(f"  {_c('36', '🧠 ' + sid)} {_short(e.payload['text'])}")
        elif e.type == TOOL_CALL:
            inp = e.payload["input"]
            detail = inp.get("file_path") or inp.get("command") or ""
            print(f"  {_c('33', '🔧 ' + sid)} {e.payload['tool']} {_short(str(detail))}")
        elif e.type == COMMIT:
            subjects = e.payload.get("subjects", [])
            print(f"  {_c('32', '⎘  committed')} {_short('; '.join(subjects))}")
        elif e.type == DIFF:
            stat = e.payload.get("stat") or "(untracked changes only)"
            untracked = e.payload.get("untracked", [])
            print(f"  {_c('35', 'Δ  diff changed')} {_short(stat.splitlines()[-1] if stat else '')}")
            if untracked:
                print(f"     untracked: {_short(', '.join(untracked))}")
