"""CodeCouncil Critic: heartbeat loop that reads the Observer's output and asks
a headless pi agent (https://pi.dev) whether anything is worth flagging.

    python3 -m critic /path/to/watched/repo [--interval 30] [--once]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from core.store import read_tail_rows, wait_for
from observer.events import now_iso
from observer.transcript import tail_new_lines

from . import agent, prompt, verify
from .render import render_error, render_quiet, render_status, render_verdict

SEED_HEURISTICS = Path(__file__).parent / "heuristics.seed.md"


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = None
        if state is not None:
            # committed_offset: how far batches have DURABLY landed (their
            # record appended to suggestions.ndjsonl). Legacy state files
            # predate this field — default it to offset so upgrading never
            # triggers a false replay. If a crash left committed_offset
            # behind offset, reset the read cursor so the lost batch replays.
            if "committed_offset" not in state:
                state["committed_offset"] = state.get("offset", 0)
            elif state["committed_offset"] < state.get("offset", 0):
                state["offset"] = state["committed_offset"]
            return state
    return {"offset": 0, "beat": 0, "committed_offset": 0}


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
    """The critic's own recent suggestions joined with how each was received.

    Both files grow unbounded over a session but only the tail is ever
    needed here (last `limit` suggestions), so both reads are bounded.
    """
    grades = {o.get("suggestion_id"): o.get("outcome") for o in read_tail_rows(outcomes_file)}
    history = []
    for r in read_tail_rows(suggestions_file):
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


PROMPTS_KEEP = 200
CASE_MATERIAL_KEEP = 200
CASE_MATERIAL_MAX_BYTES = 200_000


def _prune_dir(dir_path: Path, pattern: str, keep: int) -> None:
    """Cap a directory to its newest `keep` files (by mtime), oldest evicted first."""
    files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep]:
        old.unlink(missing_ok=True)


def save_prompt(prompts_dir: Path, verdict_id: str, text: str) -> None:
    """Audit trail: the exact prompt behind every verdict, capped to newest N."""
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{verdict_id}.txt").write_text(text, encoding="utf-8")
    _prune_dir(prompts_dir, "*.txt", PROMPTS_KEEP)


def save_case_material(cc_dir: Path, verdict_id: str, events: list[dict], latest_diff) -> None:
    """Freeze the exact batch inputs (events + latest_diff) a SUGGESTION verdict
    was judged from — what build_prompt received, not a re-derivation. The
    Reflector later harvests accepted/rebutted findings into frozen eval
    cases (evals/cases-harvested/) from this material (reflector/harvest.py),
    so the eval set grows from real outcomes instead of staying frozen.
    Skips silently if the material is unreasonably large; capped to newest N
    like save_prompt."""
    material = {"events": events, "latest_diff": latest_diff}
    text = json.dumps(material, ensure_ascii=False)
    if len(text.encode("utf-8")) > CASE_MATERIAL_MAX_BYTES:
        return
    case_dir = cc_dir / "case-material"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / f"{verdict_id}.json").write_text(text, encoding="utf-8")
    _prune_dir(case_dir, "*.json", CASE_MATERIAL_KEEP)


def normalize_file(repo: Path | None, file: str) -> str:
    """Map staging or absolute paths back to repo-relative ones.
    A finding about 'underreview/d4ab-config.py' is really about 'config.py'."""
    if not file:
        return file
    if repo:
        try:
            return str(Path(file).resolve().relative_to(Path(repo).resolve()))
        except ValueError:
            pass
    name = re.sub(r"^[0-9a-f]{6,32}-", "", Path(file).name)
    if repo:
        matches = [p for p in Path(repo).rglob(name)
                   if ".git" not in p.parts and ".codecouncil" not in p.parts]
        if len(matches) == 1:
            return str(matches[0].relative_to(repo))
    return file


def majority_session(events: list[dict]) -> str | None:
    """The dominant session id behind a judged batch, so a finding tags back to
    the session whose work produced it (diff/commit events carry no session)."""
    sessions = [e.get("session") for e in events if e.get("session")]
    if not sessions:
        return None
    return Counter(sessions).most_common(1)[0][0]


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
        "dispatched_ts": ts,
        "beat": beat,
        "session": majority_session(events),
        "heuristics_version": prompt.heuristics_version(heuristics),
        "n_events": len(events),
        "prompt_chars": len(text),
    }
    save_prompt(suggestions_file.parent / "prompts", record["id"], text)
    record.update(ask_with_retry(text, ctx))
    if record["verdict"] == "ERROR":
        render_error(beat, ts, record.get("error", "?"))
    else:
        if record["verdict"] == "SUGGESTION":
            record["suggestion"]["file"] = normalize_file(
                ctx.get("repo"), record["suggestion"].get("file", ""))
            save_case_material(suggestions_file.parent, record["id"], events,
                              ctx.get("latest_diff"))
        if record["verdict"] == "SUGGESTION" and ctx.get("verify", True):
            try:
                record["verification"] = verify.verify_finding(
                    ctx["repo"], record["suggestion"], system=ctx.get("persona"))
            except Exception as e:  # verification must never lose a finding
                record["verification"] = {"status": "error", "note": str(e)[:200]}
        render_verdict(beat, ts, record)

    record["ts"] = now_iso()
    with suggestions_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


TASK_REVIEW_COOLDOWN_S = 600

# Fields the daemon loop in main() persists to critic-state.json every beat.
# tests_run_at (Task 9): {session: iso_ts} — must survive a daemon restart or
# a test run just before a restart would silently stop counting.
PERSISTED_STATE_KEYS = (
    "offset", "committed_offset", "beat", "latest_diff", "interval",
    "review_offset", "last_task_review", "material_since_review", "tests_run_at",
)


def ask_with_retry(text: str, ctx: dict) -> dict:
    """One agent turn, retried once on transport failure or malformed reply —
    transient gateway errors were observed eating real catches."""
    last: dict = {}
    for attempt in range(2):
        try:
            reply = agent.ask(text, system=ctx.get("persona"))
        except agent.AgentError as e:
            last = {"verdict": "ERROR", "error": str(e)}
            continue
        parsed = prompt.parse_reply(reply)
        if "malformed" not in parsed:
            return parsed
        last = parsed
    return last


def should_task_review(state: dict, n_new_requests: int, now: float,
                       cooldown: float = TASK_REVIEW_COOLDOWN_S) -> bool:
    """Debounce: Stop fires every turn; a task review needs new code material
    and a quiet period since the last one."""
    if n_new_requests == 0 or not state.get("material_since_review"):
        return False
    return now - state.get("last_task_review", 0.0) >= cooldown


def recent_events(obs_file: Path, since_epoch: float) -> list[dict]:
    from datetime import datetime
    out = []
    for e in read_tail_rows(obs_file):  # bounded: task reviews only need recent events
        try:
            if datetime.fromisoformat(e["ts"]).timestamp() >= since_epoch:
                out.append(e)
        except (KeyError, ValueError):
            continue
    return out


TESTS_RUN_STICKY_MAX_AGE_S = 24 * 3600


def _ts_epoch(ts: str) -> float | None:
    """Parse an ISO timestamp to epoch seconds, or None if unparseable."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def sticky_tests_run(tests_run_at: dict | None, now_epoch: float,
                     max_age_s: float = TESTS_RUN_STICKY_MAX_AGE_S) -> str | None:
    """The most recent test-command timestamp seen anywhere the critic has
    been watching (state["tests_run_at"], keyed by session — see
    heartbeat()), bounded to max_age_s so a stale run from days ago never
    masks a truly untested change. A single value across sessions: task
    reviews carry no session tag (see task_review), so this can't be scoped
    to one — a test run in a different session can suppress the hard-negative
    fact for a review that isn't about that session's work."""
    best: str | None = None
    best_epoch = float("-inf")
    for ts in (tests_run_at or {}).values():
        t = _ts_epoch(ts)
        if t is None:
            continue
        if now_epoch - t <= max_age_s and t > best_epoch:
            best, best_epoch = ts, t
    return best


def task_review(obs_file: Path, ctx: dict, since_epoch: float) -> None:
    """One 'is it actually done?' turn. Runs on the scheduler's worker thread."""
    events = recent_events(obs_file, since_epoch)
    heuristics = ensure_heuristics(ctx["heuristics_path"])
    tests_run_sticky = ctx.get("tests_run_sticky")
    text = prompt.build_task_review(events, ctx.get("latest_diff"), heuristics,
                                    project=ctx.get("project", ""),
                                    tests_run_sticky=tests_run_sticky)
    suggestions_file = ctx["suggestions_file"]
    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": ctx["ts"],
        "dispatched_ts": ctx["ts"],
        "beat": ctx["beat"],
        "review_kind": "task",
        "heuristics_version": prompt.heuristics_version(heuristics),
        "n_events": len(events),
        "tests_run": bool(prompt.tests_run(events) or tests_run_sticky),
        "prompt_chars": len(text),
    }
    save_prompt(suggestions_file.parent / "prompts", record["id"], text)
    record.update(ask_with_retry(text, ctx))
    if record["verdict"] == "ERROR":
        render_error(ctx["beat"], ctx["ts"], record.get("error", "?"))
    else:
        render_verdict(ctx["beat"], ctx["ts"], record)
    record["ts"] = now_iso()
    with suggestions_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TurnScheduler:
    """At most one agent turn in flight; events accumulate (never drop) while
    busy or gated, and dispatch as one merged batch when possible."""

    def __init__(self, judge_fn=judge_batch, judge_every_beat: bool = False,
                 min_spacing: float = 0.0, on_committed=None):
        self.judge_fn = judge_fn
        self.judge_every_beat = judge_every_beat
        self.min_spacing = min_spacing  # floor between turn *starts*: fast beats, flat cost
        self.last_dispatch = float("-inf")  # a fresh scheduler must never start cooling
        self.pending: list[dict] = []
        self.thread: threading.Thread | None = None
        # called with the offset a dispatched batch reaches, but only once
        # judge_fn returns successfully (its record durably appended) — a
        # crash or exception mid-turn must never advance this.
        self.on_committed = on_committed
        # guards self.pending: the main thread mutates it in submit(), the
        # worker thread mutates it in _run()'s failure path (re-queueing a
        # batch whose judge_fn raised) — both must not race.
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _gate_open(self) -> bool:
        return self.judge_every_beat or any(
            e.get("type") in ("diff", "commit") for e in self.pending
        )

    def submit(self, events: list[dict], ctx: dict) -> str:
        """Returns what happened: idle | gated | busy | cooling | dispatched."""
        with self._lock:
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
        offset_at_dispatch = ctx.get("offset_now")
        self.thread = threading.Thread(
            target=self._run, args=(batch, ctx, offset_at_dispatch), daemon=True)
        self.thread.start()
        return "dispatched"

    def _run(self, batch: list[dict], ctx: dict, offset_at_dispatch) -> None:
        try:
            self.judge_fn(batch, ctx)
        except Exception as e:
            # No crash needed to lose a batch this way: a bug in judge_fn
            # (bad event shape, disk error mid-write, ...) must not silently
            # drop it either. Re-queue at the front, ahead of whatever
            # accumulated since dispatch, so order is preserved and the
            # batch replays on a later beat through the normal gate — it
            # still carries the diff/commit events that opened the gate the
            # first time. on_committed is skipped: this offset span did not
            # durably land.
            with self._lock:
                self.pending = batch + self.pending
            print(f"critic: batch judgment failed ({e}); re-queued {len(batch)} event(s)")
            return
        if self.on_committed is not None and offset_at_dispatch is not None:
            self.on_committed(offset_at_dispatch)

    def run_special(self, fn) -> bool:
        """Run a one-off turn (e.g. a task review) on the worker if it's idle."""
        if self.busy():
            return False
        self.last_dispatch = time.monotonic()
        self.thread = threading.Thread(target=fn, daemon=True)
        self.thread.start()
        return True

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

    if any(e.get("type") in ("diff", "commit") for e in events):
        state["material_since_review"] = True

    # Sticky tests-run fact (Task 9): a test command is credited to its
    # session as soon as it's seen, independent of the scheduler gate below —
    # a task review beats later can outlive this event's short review window.
    # Sessionless events (e.g. session None) are skipped: nothing to credit,
    # and it would otherwise land as a JSON "null" key on persist.
    for e in events:
        if e.get("type") == "tool_call" and e.get("session") and prompt.tests_run([e]):
            state.setdefault("tests_run_at", {})[e["session"]] = e.get("ts", ts)
    if state.get("tests_run_at"):
        # Prune stale entries at record time so the dict can't grow
        # unbounded over weeks of daemon uptime.
        now = time.time()
        state["tests_run_at"] = {s: t for s, t in state["tests_run_at"].items()
                                  if (_ts_epoch(t) or float("-inf")) >= now - TESTS_RUN_STICKY_MAX_AGE_S}

    ctx = {**ctx, "beat": beat, "ts": ts, "latest_diff": state.get("latest_diff"),
           "offset_now": state["offset"],
           "tests_run_sticky": sticky_tests_run(state.get("tests_run_at"), time.time())}
    status = scheduler.submit(events, ctx)

    # the coding agent declared itself done: consider a task-level claim review
    review_file = ctx["suggestions_file"].parent / "review-requests.ndjsonl"
    if review_file.exists():
        req_lines, state["review_offset"] = tail_new_lines(
            review_file, state.get("review_offset", 0))
        if should_task_review(state, len(req_lines), time.time(),
                              cooldown=ctx.get("task_review_cooldown", TASK_REVIEW_COOLDOWN_S)):
            since = state.get("last_task_review", time.time() - 3600)
            if scheduler.run_special(
                lambda: task_review(obs_file, ctx, since_epoch=since)
            ):
                state["last_task_review"] = time.time()
                state["material_since_review"] = False
                render_status(beat, ts, "task review dispatched — agent claimed done")
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
    ap.add_argument("--judge-every-beat", action="store_true",
                    help="also judge batches with no code change (reasoning-only)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip repro verification of findings")
    ap.add_argument("--task-review-cooldown", type=float, default=TASK_REVIEW_COOLDOWN_S,
                    help="minimum seconds between task reviews (default 600)")
    args = ap.parse_args(argv)

    cc = args.repo.resolve() / ".codecouncil"
    obs_file = cc / "observations.ndjsonl"
    if not wait_for(obs_file, "is the observer running?", args.once):
        return 2

    state_path = cc / "critic-state.json"
    state = load_state(state_path)
    print(f"critic: reading {obs_file}")
    model = os.environ.get("COUNCIL_MODEL", "pi's default model")
    print(f"critic: judging via headless pi ({model}) every {args.interval:g}s")

    ctx = {
        "repo": args.repo.resolve(),
        "heuristics_path": cc / "heuristics.md",
        "suggestions_file": cc / "suggestions.ndjsonl",
        "persona": agent.CRITIC_PERSONA.read_text(encoding="utf-8"),
        "project": project_context(args.repo.resolve()),
        "verify": not args.no_verify,
        "task_review_cooldown": args.task_review_cooldown,
    }
    def _on_committed(offset: int) -> None:
        # Runs on the scheduler's worker thread (called from TurnScheduler._run
        # after judge_fn succeeds), mutating the state dict the main thread
        # owns. A plain int assignment is atomic under the GIL, so this is
        # safe without a lock; the daemon persists state to disk on the main
        # loop below.
        state["committed_offset"] = offset

    scheduler = TurnScheduler(judge_every_beat=args.judge_every_beat,
                              min_spacing=args.turn_spacing,
                              on_committed=_on_committed)
    state["interval"] = args.interval
    try:
        while True:
            heartbeat(obs_file, state, scheduler, ctx)
            if args.once:
                # drain first so a clean --once exit persists the
                # committed_offset the drained batch actually reached,
                # rather than a stale one that would replay it needlessly.
                scheduler.drain({**ctx, "beat": state["beat"], "ts": now_iso(),
                                 "latest_diff": state.get("latest_diff"),
                                 "offset_now": state["offset"]})
            state_path.write_text(json.dumps(
                {k: state[k] for k in PERSISTED_STATE_KEYS if k in state}
            ), encoding="utf-8")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ncritic: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
