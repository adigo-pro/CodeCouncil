#!/usr/bin/env python3
"""Claude Code hook entry point for CodeCouncil peer review.

Registered (by hooks/install.py) as a PostToolUse and Stop hook in a watched
repo. Reads the hook event from stdin, checks .codecouncil/ in that repo for
undelivered Critic suggestions, and prints injection/block JSON to stdout.

Invariant: fail open. Whatever goes wrong, exit 0 with no output — this script
must never be able to break a developer's session.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.store import read_tail_rows as read_suggestions
from critic.receipt import parse_test_integrity
from hooks import ledger as ledger_mod
from hooks.logic import decide, gate_pending, resolve_gate_seconds


@contextlib.contextmanager
def _locked(lock_path: Path):
    """Exclusive lock around a load->decide->save span on delivered.json.

    PostToolUse hooks run as fresh subprocesses per event, and multiple
    concurrent Claude Code sessions on the same repo can fire them at once;
    without this, two processes can interleave and double-deliver or lose a
    delivery mark. Must stay fail-open: any error acquiring or using the lock
    (no fcntl on this platform, the sidecar can't be created, ...) falls back
    to proceeding without it rather than raising.
    """
    fh = None
    try:
        import fcntl
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except OSError:
                pass


def _read_critic_state(path: Path) -> dict | None:
    """Tolerant read of critic-state.json's persisted fields, mirroring the
    tolerant parsing critic.main.load_state does (not imported directly:
    peer_hook only shares small cross-loop utilities, per CLAUDE.md). Returns
    None on anything short of a clean dict — missing file, unreadable, or
    malformed JSON — so the done-gate's poll loop can treat that as "critic
    state unknown" and stop waiting rather than guess at offsets."""
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


# A Stop-time gate wait only needs to know whether recent unjudged material
# includes a diff/commit — reaching back further than this into an enormous
# uncommitted backlog wouldn't change the answer and would just slow the
# hook down every poll. 512KB comfortably covers any realistic backlog's
# recent tail.
_GATE_SCAN_CAP = 512_000


def _read_unjudged_events(obs_file: Path, committed_offset: int) -> list[dict]:
    """Tolerant NDJSON parse of observations.ndjsonl from `committed_offset`
    to EOF — the material the critic hasn't durably committed past yet (same
    committed_offset the critic itself persists in critic-state.json). Skips
    unparseable/partial lines per repo convention (core.store). If that slice
    is bigger than _GATE_SCAN_CAP, only the LAST _GATE_SCAN_CAP bytes are
    scanned (a possibly-truncated first line in that tail is dropped)."""
    try:
        size = obs_file.stat().st_size
        if size <= committed_offset:
            return []
        start = max(committed_offset, size - _GATE_SCAN_CAP)
        with obs_file.open("rb") as f:
            f.seek(start)
            if start > committed_offset:
                f.readline()  # discard the partial line the seek landed inside
            data = f.read()
    except OSError:
        return []
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _maybe_wait_for_critic(cc: Path, event: dict) -> bool:
    """Task 1 done-gate: optionally hold a Stop declaration open while the
    critic catches up on material it hasn't judged yet, so a finding landing
    in that window blocks like any other pending finding instead of being
    missed by a session that finishes in under one judge cycle.

    Off by default (COUNCIL_GATE_SECONDS env, or "gate_seconds" in the
    user-level ~/.codecouncil/config.json — core.config is a small shared
    cross-loop utility, unlike the watched repo's own .codecouncil/ that this
    function otherwise reads). At most one wait per session (ledger key
    "gate"). Any exception anywhere -> behave exactly as if the gate were
    off; this function must never be the reason the hook fails to fail open.

    Returns True iff a wait was actually entered for this Stop (the gate
    resolved on AND there was real unjudged material to wait for) — run()
    threads this into decide()'s `gated` param so the Stop block-severity
    floor drops to medium+ only when the gate was genuinely active this
    turn (run-3-levers Task 1). Gate off, already spent this session, or
    nothing pending -> False, same as any exception along the way.

    Locking: delivered.lock is shared with every hook call in every
    concurrent session on this repo. The poll loop below can run for up to
    gate_seconds (default cap 120s) and must NEVER hold that lock while it
    sleeps — otherwise one session's Stop wait wedges every other session's
    PostToolUse behind it. So the lock is only ever held for two short spans:
    (1) an initial check of the ledger's gate-used flag plus whether there is
    anything to wait for at all, and (2) — after the (unlocked) poll loop
    finishes — marking the gate used. A session that races another
    invocation of itself between these two spans can wait twice; that's a
    small, bounded, harmless race, not a wedge, and is accepted deliberately.
    """
    try:
        env_value = os.environ.get("COUNCIL_GATE_SECONDS")
        config_value = load_config().get("gate_seconds")
        gate_seconds = resolve_gate_seconds(env_value, config_value)
        if gate_seconds <= 0:
            return False
        session_key = event.get("session_id") or ""
        ledger_path = cc / "delivered.json"
        lock_path = cc / "delivered.lock"
        obs_file = cc / "observations.ndjsonl"
        state_path = cc / "critic-state.json"

        def _pending() -> bool:
            state = _read_critic_state(state_path)
            if state is None:
                return False  # unknown critic state -> treat as caught up
            committed = state.get("committed_offset", 0)
            return gate_pending(_read_unjudged_events(obs_file, committed))

        with _locked(lock_path):
            ledger = ledger_mod.load(ledger_path)
            if ledger_mod.gate_used(ledger, session_key):
                return False
            if not _pending():
                return False

        # Poll OUTSIDE any lock — see docstring above.
        deadline = time.time() + gate_seconds
        while _pending() and time.time() < deadline:
            time.sleep(1)

        with _locked(lock_path):
            ledger = ledger_mod.load(ledger_path)
            ledger_mod.mark_gate(ledger, session_key, time.time())
            ledger_mod.save(ledger_path, ledger)
        return True  # a wait was genuinely entered this Stop
    except Exception:
        return False  # fail open: proceed exactly as if the gate were off


def run(stdin_text: str) -> str | None:
    event = json.loads(stdin_text)
    cc = Path(event["cwd"]) / ".codecouncil"
    suggestions_file = cc / "suggestions.ndjsonl"
    gated = False
    if event.get("hook_event_name") == "Stop" and cc.is_dir():
        # the coding agent thinks it's done — ask the critic for a task-level
        # claim review (the critic daemon debounces; writing is best-effort)
        try:
            with (cc / "review-requests.ndjsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(),
                                    "session": event.get("session_id", "")}) + "\n")
        except OSError:
            pass
        # done-gate (Task 1): optionally hold "done" open while the critic
        # catches up — must run BEFORE the suggestions/receipts reads below
        # so a finding that lands during the wait is delivered this turn.
        # Its return says whether a wait was genuinely entered this Stop;
        # that's threaded into decide()'s `gated` so the block-severity
        # floor drops to medium+ only when the gate was actually active
        # (run-3-levers Task 1) — a finished session has no later
        # PostToolUse to deliver a medium finding through.
        gated = _maybe_wait_for_critic(cc, event)
    receipts_dir = cc / "receipts"
    if not suggestions_file.exists() and not receipts_dir.is_dir():
        return None
    receipts = _attach_test_integrity(_list_receipts(receipts_dir))
    ledger_path = cc / "delivered.json"
    lock_path = cc / "delivered.lock"
    with _locked(lock_path):
        ledger = ledger_mod.load(ledger_path)
        output = decide(event, read_suggestions(suggestions_file), ledger, time.time(),
                        receipts=receipts, gated=gated)
        if output is None:
            return None
        ledger_mod.save(ledger_path, ledger)  # persist only when something was delivered
    return json.dumps(output)


def _list_receipts(receipts_dir: Path) -> list[dict]:
    """Receipt files (critic.receipt.write_receipt output), newest first —
    the only fs access for receipt announcement; decide() stays pure."""
    if not receipts_dir.is_dir():
        return []
    try:
        files = sorted(receipts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [{"name": p.name, "path": str(p)} for p in files]


def _attach_test_integrity(receipts: list[dict]) -> list[dict]:
    """Read only the newest receipt's content and parse out its
    test_integrity block (Task 2's Stop gate needs it; older entries stay
    name/path-only, same cost as before this feature). Fail open: any
    read/parse error just leaves the entry without a "test_integrity" key,
    same as a receipt written before this feature existed — decide() treats
    that as nothing to block on."""
    if not receipts:
        return receipts
    try:
        ti = parse_test_integrity(Path(receipts[0]["path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ti = None
    if ti is None:
        return receipts
    return [{**receipts[0], "test_integrity": ti}, *receipts[1:]]


def main() -> int:
    try:
        out = run(sys.stdin.read())
        if out:
            print(out)
    except Exception:
        pass  # fail open, always
    return 0


if __name__ == "__main__":
    sys.exit(main())
