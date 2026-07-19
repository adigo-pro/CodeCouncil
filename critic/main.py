"""CodeCouncil Critic: heartbeat loop that reads the Observer's output and asks
an OpenClaw agent (in the NemoClaw sandbox) whether anything is worth flagging.

    python3 -m critic /path/to/watched/repo [--interval 30] [--once]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from observer.events import now_iso
from observer.transcript import tail_new_lines

from . import openclaw, prompt
from .render import render_error, render_quiet, render_verdict

SEED_HEURISTICS = Path(__file__).parent / "heuristics.seed.md"


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"offset": 0, "beat": 0}


def project_context(repo: Path) -> str:
    """A short identity header so the critic knows what repo it is judging."""
    lines = [f"PROJECT: {repo.name} ({repo})"]
    try:
        entries = sorted(
            p.name + ("/" if p.is_dir() else "")
            for p in repo.iterdir() if not p.name.startswith(".")
        )[:30]
        lines.append("TOP-LEVEL: " + " ".join(entries))
    except OSError:
        pass
    readme = repo / "README.md"
    if readme.exists():
        excerpt = " ".join(
            l.strip() for l in readme.read_text(encoding="utf-8", errors="replace").splitlines()[:20]
            if l.strip()
        )
        lines.append("README: " + excerpt[:600])
    return "\n".join(lines)


def ensure_heuristics(path: Path) -> str:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SEED_HEURISTICS, path)
    return path.read_text(encoding="utf-8")


def heartbeat(obs_file: Path, state: dict, heuristics_path: Path,
              suggestions_file: Path, sandbox: str, agent: str,
              project: str = "") -> None:
    state["beat"] += 1
    beat, ts = state["beat"], now_iso()

    lines, state["offset"] = tail_new_lines(obs_file, state["offset"])
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    diffs = [e for e in events if e["type"] == "diff"]
    if diffs:
        state["latest_diff"] = diffs[-1]

    if not events:
        render_quiet(beat, ts)
        return

    heuristics = ensure_heuristics(heuristics_path)
    text = prompt.build_prompt(events, state.get("latest_diff"), heuristics, project=project)
    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": ts,
        "beat": beat,
        "heuristics_version": prompt.heuristics_version(heuristics),
        "n_events": len(events),
    }
    try:
        session = f"critic-{uuid.uuid4().hex[:12]}"  # unique per call: each judgment starts clean
        reply = openclaw.ask(text, sandbox=sandbox, agent=agent, session=session)
        record.update(prompt.parse_reply(reply))
        render_verdict(beat, ts, record)
    except openclaw.AgentError as e:
        record.update({"verdict": "ERROR", "error": str(e)})
        render_error(beat, ts, str(e))

    with suggestions_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="critic", description=__doc__)
    ap.add_argument("repo", type=Path, help="path to the repo being watched by the observer")
    ap.add_argument("--interval", type=float, default=30.0, help="heartbeat seconds (default 30)")
    ap.add_argument("--once", action="store_true", help="run a single heartbeat and exit")
    ap.add_argument("--sandbox", default="codecouncil", help="NemoClaw sandbox name")
    ap.add_argument("--agent", default="critic", help="OpenClaw agent id in the sandbox")
    args = ap.parse_args(argv)

    cc = args.repo.resolve() / ".codecouncil"
    obs_file = cc / "observations.ndjsonl"
    if not obs_file.exists():
        print(f"error: {obs_file} not found — is the observer running?", file=sys.stderr)
        return 2

    state_path = cc / "critic-state.json"
    state = load_state(state_path)
    print(f"critic: reading {obs_file}")
    print(f"critic: judging via `nemoclaw {args.sandbox} agent --agent {args.agent}` every {args.interval:g}s")

    project = project_context(args.repo.resolve())
    try:
        while True:
            heartbeat(obs_file, state, cc / "heuristics.md", cc / "suggestions.ndjsonl",
                      args.sandbox, args.agent, project=project)
            state_path.write_text(json.dumps(
                {k: state[k] for k in ("offset", "beat", "latest_diff") if k in state}
            ), encoding="utf-8")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ncritic: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
