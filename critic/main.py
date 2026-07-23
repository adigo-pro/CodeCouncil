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

from core import knowledge
from core.store import read_tail_rows, wait_for
from observer.events import now_iso
from observer.transcript import tail_new_lines
# Loop-boundary note: this is observer-side, but CLAUDE.md carves out an
# exception for small shared utilities across loops (alongside core.store,
# observer.events, observer.transcript, critic.agent). _touched_paths is a
# 6-line pure function (no I/O) parsing "+++ b/<path>" diff headers; reusing
# it here keeps reviewed_files' diff-path parsing identical to the Observer's
# own touched_contents derivation instead of drifting from a second copy.
from observer.gitwatch import _touched_paths

from . import agent, prompt, receipt, verify
from .render import render_error, render_quiet, render_status, render_verdict

SEED_HEURISTICS = Path(__file__).parent / "heuristics.seed.md"

# Path-jailed, read-only tools for judgment turns (Task 4 + hardening): the
# model may look before it flags, but a judgment turn must never be able to
# mutate the developer's repo (never "bash", never "edit"/"write") NOR read
# outside it. pi's *builtin* read/grep/find/ls resolve absolute and "~"
# paths straight through regardless of cwd — the --tools allowlist only
# gates which tool names are active, not which paths a tool may touch — so
# offering them here with cwd=<watched repo> would let the model read
# ~/.codecouncil/env (cleartext NVIDIA_API_KEY) or ~/.ssh/*. These four are
# a separate, jailed implementation (critic/pi_extensions/repo_tools.mjs):
# every path is realpath-resolved and checked against the repo root before
# any filesystem access, with .git/.codecouncil excluded (the critic's own
# workspace stays invisible, same discipline as verify.py's staging dir).
# Verification turns keep their own, separately-sandboxed tool set
# (critic/verify.py, throwaway staging directory, never the developer's repo).
JUDGE_TOOLS = "repo_read,repo_grep,repo_find,repo_ls"


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


# CLAUDE.md excerpt cap for the REPO INVARIANTS block below (Task 3).
CLAUDE_MD_EXCERPT_CHARS = 1200


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
        try:
            readme_lines = readme.read_text(encoding="utf-8", errors="replace").splitlines()
            excerpt = " ".join(
                line.strip() for line in readme_lines[:20] if line.strip()
            )
            lines.append("README: " + excerpt[:600])
        except OSError:
            pass
    # REPO INVARIANTS rides in EVERY prompt — this "project" string is passed
    # to both build_prompt and build_task_review (Task 3) unconditionally,
    # not gated behind is_plan_material — because invariant-aware criticism
    # is useful for any change, not just plan/design documents (e.g. "does
    # this diff cross a loop boundary CLAUDE.md forbids?"). Plan review
    # (prompt.PLAN_REVIEW_ADDENDUM) additionally tells the model to check a
    # plan document's own steps against this same block.
    claude_md = repo / "CLAUDE.md"
    if claude_md.exists():
        try:
            text = claude_md.read_text(encoding="utf-8", errors="replace")
            if len(text) > CLAUDE_MD_EXCERPT_CHARS:
                text = text[:CLAUDE_MD_EXCERPT_CHARS] + f"… [{len(text)} chars total]"
            lines.append("REPO INVARIANTS:\n" + text)
        except OSError:
            pass
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
    """Freeze the exact batch inputs (events + latest_diff) a verdict was
    judged from — what build_prompt received, not a re-derivation. Saved for
    every non-ERROR verdict (PASS included, not just SUGGESTION): the
    Reflector harvests both accepted/rebutted findings AND missed PASSes
    (a PASS later contradicted by a fix commit, reflector/misses.py) into
    frozen eval cases (evals/cases-harvested/) from this material
    (reflector/harvest.py), so the eval set grows from real outcomes instead
    of staying frozen. A missed PASS can only be harvested if its judgment
    packet was kept, hence saving on PASS too. Skips silently if the material
    is unreasonably large; capped to newest N like save_prompt."""
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


def reviewed_files(latest_diff: dict | None, events: list[dict]) -> list[str]:
    """Repo-relative paths a verdict actually covered: the sorted union of
    touched_contents keys, untracked paths, and paths parsed from the raw
    diff's `+++ b/<path>` headers. The third source matters on its own:
    touched_contents (observer/gitwatch.py's _read_touched) caps by total
    chars and excluded prefixes, so a file can be in the diff yet missing
    from touched_contents — re-parsing the diff recovers it.

    Also unions in paths parsed from every "commit" event's diff in this
    judged batch: a file written-and-committed within a single beat carries
    no "diff" event at all (only "commit"), so without this it would never
    appear in any verdict's reviewed_files and could never be miss-graded.
    Empty when there's nothing to review yet."""
    paths: set[str] = set()
    if latest_diff:
        payload = latest_diff.get("payload", {}) or {}
        paths.update(payload.get("touched_contents", {}) or {})
        paths.update(payload.get("untracked", []) or [])
        paths.update(_touched_paths(payload.get("diff", "") or ""))
    for e in events:
        if e.get("type") == "commit":
            payload = e.get("payload") or {}
            paths.update(_touched_paths(payload.get("diff", "") or ""))
    return sorted(paths)


def judge_batch(events: list[dict], ctx: dict) -> None:
    """One model judgment over a batch. Runs on the scheduler's worker thread;
    sole writer of the suggestions file."""
    beat, ts = ctx["beat"], ctx["ts"]
    suggestions_file = ctx["suggestions_file"]
    heuristics = ensure_heuristics(ctx["heuristics_path"])
    history = verdict_history(suggestions_file, suggestions_file.parent / "outcomes.ndjsonl")
    kb = knowledge.load(suggestions_file.parent)  # fresh every call, same pattern as heuristics
    text = prompt.build_prompt(events, ctx.get("latest_diff"), heuristics,
                               project=ctx.get("project", ""), verdict_history=history,
                               knowledge=kb)
    record = {
        "id": uuid.uuid4().hex[:12],
        "ts": ts,
        "dispatched_ts": ts,
        "beat": beat,
        "session": majority_session(events),
        "heuristics_version": prompt.heuristics_version(heuristics),
        "n_events": len(events),
        "prompt_chars": len(text),
        "reviewed_files": reviewed_files(ctx.get("latest_diff"), events),
    }
    save_prompt(suggestions_file.parent / "prompts", record["id"], text)
    primary_parsed = ask_with_retry(text, ctx)
    if ctx.get("prober") and ctx.get("verify", True):
        # Council mode: ask a second, recall-oriented model the SAME prompt.
        # Measured basis (docs/benchmarks/): the primary (precision anchor)
        # has 0 false positives but misses catches; the prober catches
        # everything but false-positives on clean changes. A prober-only
        # finding is therefore only trustworthy once verify.verify_finding
        # (below, gated on record["verdict"] == "SUGGESTION") has a chance to
        # run a repro against it — an unverifiable prober is a
        # false-positive machine per the bake-off. That's why the prober is
        # gated on ctx["verify"] here too, not just ctx["prober"]: with
        # verification disabled there is no repro step to catch a prober
        # false-positive, so we skip the prober call entirely and fall back
        # to exactly today's single-model flow (no "council" key at all)
        # rather than deliver an unverifiable guess into the record.
        try:
            prober_parsed = ask_with_retry(text, ctx, model=ctx["prober"])
        except Exception as e:  # a prober failure must never cost the primary verdict
            prober_parsed = {"verdict": "ERROR", "error": str(e)[:200]}
        merged, council = merge_council(primary_parsed, prober_parsed)
        council["prober_model"] = ctx["prober"]
        record.update(merged)
        record["council"] = council
    else:
        record.update(primary_parsed)
    if record["verdict"] == "ERROR":
        render_error(beat, ts, record.get("error", "?"))
    else:
        # Case material now saved for every non-ERROR verdict (PASS included)
        # — the Reflector can only harvest a missed PASS into an eval case
        # if the PASS's judgment packet was kept, not just SUGGESTIONs.
        save_case_material(suggestions_file.parent, record["id"], events,
                          ctx.get("latest_diff"))
        if record["verdict"] == "SUGGESTION":
            record["suggestion"]["file"] = normalize_file(
                ctx.get("repo"), record["suggestion"].get("file", ""))
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


def ask_with_retry(text: str, ctx: dict, model: str | None = None) -> dict:
    """One agent turn, retried once on transport failure or malformed reply —
    transient gateway errors were observed eating real catches.

    model: explicit "provider/model" override for this call, passed straight
        through to agent.ask (see its docstring). None = today's ambient
        resolution (COUNCIL_MODEL / NVIDIA default) — this is how council
        mode's prober call in judge_batch asks the SAME prompt of a second
        model without touching the primary call site or os.environ."""
    last: dict = {}
    repo = ctx.get("repo")
    for attempt in range(2):
        try:
            if repo:
                reply = agent.ask(text, system=ctx.get("persona"),
                                  tools=JUDGE_TOOLS, cwd=str(repo), model=model)
            else:
                reply = agent.ask(text, system=ctx.get("persona"), model=model)
        except agent.AgentError as e:
            last = {"verdict": "ERROR", "error": str(e)}
            continue
        parsed = prompt.parse_reply(reply)
        if "malformed" not in parsed:
            return parsed
        last = parsed
    return last


def resolve_prober(flag: str | None, env: dict) -> str | None:
    """Council mode's model precedence for ctx["prober"]: an explicit
    --prober flag wins, then COUNCIL_PROBER from the environment, else no
    council (single-model flow, unchanged from before council mode existed).
    Pure — no I/O — so main() and tests share one place this rule lives."""
    return flag or env.get("COUNCIL_PROBER") or None


def merge_council(primary: dict, prober: dict) -> tuple[dict, dict]:
    """Pure merge of two parsed verdicts (ask_with_retry's return shape) into
    one chosen verdict + a council info dict. No I/O, no model calls — the
    whole point is that this unit-tests without a stub.

    Six-combo table (measured basis in docs/benchmarks/: primary is a
    precision anchor with 0 false positives but misses catches; prober has
    full recall but false-positives on clean changes):
      primary SUGGESTION + prober SUGGESTION -> primary's suggestion, "both"
      primary SUGGESTION + prober PASS/ERROR -> primary's,   "primary-only"
      primary PASS       + prober SUGGESTION -> prober's suggestion, "prober-only"
      primary PASS       + prober PASS       -> primary's PASS, "both"
      primary PASS       + prober ERROR      -> primary's PASS, "primary-only"

    Deliberately does NOT stamp "prober_model" into the returned council
    dict: that's a static config value the caller (judge_batch) already has
    in ctx["prober"], not something this function needs to know about — the
    caller fills it in after the call, keeping this function free of any
    ctx/config coupling.
    """
    primary_verdict = primary.get("verdict")
    prober_verdict = prober.get("verdict")
    if primary_verdict == "SUGGESTION":
        chosen = primary
        agreement = "both" if prober_verdict == "SUGGESTION" else "primary-only"
    elif prober_verdict == "SUGGESTION":
        chosen = prober
        agreement = "prober-only"
    else:
        chosen = primary
        agreement = "both" if prober_verdict == "PASS" else "primary-only"
    return chosen, {"prober_verdict": prober_verdict, "agreement": agreement}


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
    suggestions_file = ctx["suggestions_file"]
    kb = knowledge.load(suggestions_file.parent)  # fresh every call, same pattern as heuristics
    text = prompt.build_task_review(events, ctx.get("latest_diff"), heuristics,
                                    project=ctx.get("project", ""),
                                    tests_run_sticky=tests_run_sticky,
                                    knowledge=kb)
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

    # The human-facing artifact (Task 10): claims vs mechanically verified
    # facts vs findings raised this session. Never let a receipt failure lose
    # a review that already landed durably above.
    try:
        tests_fact = f"MECHANICAL FACT — {prompt.mechanical_fact(events, tests_run_sticky)}"
        receipt_path = receipt.write_receipt(
            suggestions_file.parent, {**ctx, "since_epoch": since_epoch}, events, record,
            tests_fact)
        print(f"critic: receipt written to {receipt_path}")
    except Exception as e:
        print(f"critic: receipt failed ({e})")


MAX_BATCH_RETRIES = 3  # a batch failing this many dispatches in a row is dropped, not requeued forever


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
        # Consecutive judge_fn failures since the last successful dispatch.
        # A single scheduler-wide counter (not one per batch) is enough
        # because dispatch is fully serialized — only one batch can ever be
        # "the" failing one at a time — and it resets to 0 the moment any
        # dispatch succeeds, so an old poisoned batch's failures never carry
        # over to unfairly doom an unrelated later batch.
        self._requeue_count = 0

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
            self._requeue_count += 1
            if self._requeue_count >= MAX_BATCH_RETRIES:
                # A batch that fails MAX_BATCH_RETRIES dispatches in a row is
                # poisoned, not unlucky — requeueing it again would spin
                # forever (unbounded pending growth, the observation stream
                # never advances). Drop it. committed_offset is deliberately
                # left untouched (see submit()/on_committed above): on a
                # daemon restart the dropped span will simply be replayed
                # and re-judged once, which is the lesser evil next to
                # letting a poison batch wedge the scheduler permanently.
                print(f"critic: batch dropped after {MAX_BATCH_RETRIES} failed judgments "
                      f"({e}) — events lost: {len(batch)}")
                self._requeue_count = 0
                return
            with self._lock:
                self.pending = batch + self.pending
            print(f"critic: batch judgment failed ({e}); re-queued {len(batch)} event(s) "
                  f"(attempt {self._requeue_count}/{MAX_BATCH_RETRIES})")
            return
        self._requeue_count = 0
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
    ap.add_argument("--prober", default=None,
                    help="council mode: second model asked the same prompt, "
                         "recall-oriented, verified before delivery (or COUNCIL_PROBER env)")
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

    prober = resolve_prober(args.prober, agent.local_env())
    if prober:
        print(f"critic: council mode — prober {prober}")

    ctx = {
        "repo": args.repo.resolve(),
        "heuristics_path": cc / "heuristics.md",
        "suggestions_file": cc / "suggestions.ndjsonl",
        "persona": agent.CRITIC_PERSONA.read_text(encoding="utf-8"),
        "project": project_context(args.repo.resolve()),
        "verify": not args.no_verify,
        "task_review_cooldown": args.task_review_cooldown,
        "prober": prober,
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
