"""CodeCouncil Critic: heartbeat loop that reads the Observer's output and asks
an OpenClaw agent (in the NemoClaw sandbox) whether anything is worth flagging.

    python3 -m critic /path/to/watched/repo [--interval 30] [--once]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from observer.events import now_iso
from observer.transcript import tail_new_lines

from . import openclaw, prompt
from .render import render_error, render_quiet, render_status, render_verdict

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


def verdict_history(suggestions_file: Path, outcomes_file: Path, limit: int = 5) -> list[dict]:
    """The critic's own recent suggestions joined with how each was received."""
    def rows(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    grades = {o.get("suggestion_id"): o.get("outcome") for o in rows(outcomes_file)}
    history = []
    for r in rows(suggestions_file):
        if r.get("verdict") != "SUGGESTION":
            continue
        s = r["suggestion"]
        history.append({
            "outcome": grades.get(r.get("id"), "pending"),
            "file": s["file"], "line": s.get("line"), "issue": s["issue"],
        })
    return history[-limit:]


def ensure_heuristics(path: Path) -> str:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SEED_HEURISTICS, path)
    return path.read_text(encoding="utf-8")


def judge_batch(events: list[dict], ctx: dict) -> None:
    """One model judgment over a batch. Runs on the scheduler's worker thread;
    sole writer of the suggestions file."""
    beat, ts = ctx["beat"], ctx["ts"]
    suggestions_file = ctx["suggestions_file"]
    heuristics = ensure_heuristics(ctx["heuristics_path"])
    history = verdict_history(suggestions_file, suggestions_file.parent / "outcomes.ndjsonl")
    text = prompt.build_prompt(events, ctx.get("latest_diff"), heuristics,
                               project=ctx.get("project", ""), verdict_history=history)
    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": ts,
        "beat": beat,
        "heuristics_version": prompt.heuristics_version(heuristics),
        "n_events": len(events),
        "prompt_chars": len(text),
    }
    # audit trail: the exact prompt behind every verdict, keyed by verdict id
    prompts_dir = suggestions_file.parent / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{record['id']}.txt").write_text(text, encoding="utf-8")
    try:
        session = f"critic-{uuid.uuid4().hex[:12]}"  # unique per call: each judgment starts clean
        reply = openclaw.ask(text, sandbox=ctx["sandbox"], agent=ctx["agent"], session=session)
        record.update(prompt.parse_reply(reply))
        render_verdict(beat, ts, record)
    except openclaw.AgentError as e:
        record.update({"verdict": "ERROR", "error": str(e)})
        render_error(beat, ts, str(e))

    with suggestions_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TurnScheduler:
    """At most one agent turn in flight; events accumulate (never drop) while
    busy or gated, and dispatch as one merged batch when possible."""

    def __init__(self, judge_fn=judge_batch, judge_every_beat: bool = False,
                 min_spacing: float = 0.0):
        self.judge_fn = judge_fn
        self.judge_every_beat = judge_every_beat
        self.min_spacing = min_spacing  # floor between turn *starts*: fast beats, flat cost
        self.last_dispatch = float("-inf")  # a fresh scheduler must never start cooling
        self.pending: list[dict] = []
        self.thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _gate_open(self) -> bool:
        return self.judge_every_beat or any(
            e.get("type") in ("diff", "commit") for e in self.pending
        )

    def submit(self, events: list[dict], ctx: dict) -> str:
        """Returns what happened: idle | gated | busy | cooling | dispatched."""
        self.pending.extend(events)
        if not self.pending:
            return "idle"
        if not self._gate_open():
            return "gated"
        if self.busy():
            return "busy"
        if time.monotonic() - self.last_dispatch < self.min_spacing:
            return "cooling"
        self.last_dispatch = time.monotonic()
        batch, self.pending = self.pending, []
        self.thread = threading.Thread(target=self.judge_fn, args=(batch, ctx), daemon=True)
        self.thread.start()
        return "dispatched"

    def drain(self, ctx: dict) -> None:
        """Finish in-flight work and flush a dispatchable remainder (for --once)."""
        if self.thread:
            self.thread.join()
        if self.pending and self._gate_open():
            self.last_dispatch = float("-inf")  # --once must not wait out the cooldown
            self.submit([], ctx)
            if self.thread:
                self.thread.join()


def heartbeat(obs_file: Path, state: dict, scheduler: TurnScheduler, ctx: dict) -> str:
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

    ctx = {**ctx, "beat": beat, "ts": ts, "latest_diff": state.get("latest_diff")}
    status = scheduler.submit(events, ctx)
    if status == "idle":
        render_quiet(beat, ts)
    elif status == "gated":
        render_status(beat, ts, f"{len(scheduler.pending)} event(s) held — no code change yet")
    elif status == "busy":
        render_status(beat, ts, f"turn in flight — {len(scheduler.pending)} event(s) queued")
    elif status == "cooling":
        render_status(beat, ts, f"cooling down — {len(scheduler.pending)} event(s) queued")
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="critic", description=__doc__)
    ap.add_argument("repo", type=Path, help="path to the repo being watched by the observer")
    ap.add_argument("--interval", type=float, default=10.0, help="heartbeat seconds (default 10)")
    ap.add_argument("--turn-spacing", type=float, default=45.0,
                    help="minimum seconds between model turn starts (default 45)")
    ap.add_argument("--once", action="store_true", help="run a single heartbeat and exit")
    ap.add_argument("--sandbox", default="codecouncil", help="NemoClaw sandbox name")
    ap.add_argument("--agent", default="critic", help="OpenClaw agent id in the sandbox")
    ap.add_argument("--judge-every-beat", action="store_true",
                    help="also judge batches with no code change (reasoning-only)")
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

    ctx = {
        "heuristics_path": cc / "heuristics.md",
        "suggestions_file": cc / "suggestions.ndjsonl",
        "sandbox": args.sandbox,
        "agent": args.agent,
        "project": project_context(args.repo.resolve()),
    }
    scheduler = TurnScheduler(judge_every_beat=args.judge_every_beat,
                              min_spacing=args.turn_spacing)
    state["interval"] = args.interval
    try:
        while True:
            heartbeat(obs_file, state, scheduler, ctx)
            state_path.write_text(json.dumps(
                {k: state[k] for k in ("offset", "beat", "latest_diff", "interval") if k in state}
            ), encoding="utf-8")
            if args.once:
                scheduler.drain({**ctx, "beat": state["beat"], "ts": now_iso(),
                                 "latest_diff": state.get("latest_diff")})
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ncritic: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
