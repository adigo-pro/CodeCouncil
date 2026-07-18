"""CodeCouncil Observer: heartbeat daemon that pairs Claude Code's stated intent
(transcript reasoning + tool calls) with what actually changed (git diff).

    python -m observer /path/to/watched/repo [--interval 30] [--once] [--from-start]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import gitwatch, transcript
from .events import DIFF, Event, EventLog, now_iso
from .render import render_beat
from .state import State


def heartbeat(repo: Path, project_dir: Path, state: State, log: EventLog) -> list[Event]:
    state.beat += 1
    events = transcript.collect(project_dir, state.offsets, state.beat)

    snapshot = gitwatch.capture(repo)
    fp = gitwatch.fingerprint(snapshot)
    if fp != state.last_diff_hash:
        state.last_diff_hash = fp
        events.append(Event(beat=state.beat, type=DIFF, payload=snapshot))

    log.append(events)
    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="observer", description=__doc__)
    ap.add_argument("repo", type=Path, help="path to the repo being coded in")
    ap.add_argument("--interval", type=float, default=30.0, help="heartbeat seconds (default 30)")
    ap.add_argument("--once", action="store_true", help="run a single heartbeat and exit")
    ap.add_argument(
        "--from-start",
        action="store_true",
        help="ignore saved offsets and replay transcripts from the beginning",
    )
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    project_dir = transcript.find_project_dir(repo)
    if project_dir is None:
        print(
            f"error: no Claude Code transcripts found for {repo}\n"
            f"       (looked in {transcript.PROJECTS_ROOT})",
            file=sys.stderr,
        )
        return 2

    out_dir = repo / ".codecouncil"
    state_path = out_dir / "state.json"
    state = State.load(state_path)
    if args.from_start:
        state = State()
    log = EventLog(out_dir / "observations.ndjsonl")

    print(f"observer: watching {project_dir.name}")
    print(f"observer: repo {repo} · every {args.interval:g}s · log {log.path}")

    try:
        while True:
            events = heartbeat(repo, project_dir, state, log)
            state.save(state_path)
            render_beat(state.beat, now_iso(), events)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nobserver: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
