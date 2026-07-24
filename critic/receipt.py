"""Session receipt: the human-facing artifact from a task review.

Today only the coding agent sees the Critic's findings (via hooks). When the
agent declares work done (critic.main.task_review), write_receipt renders a
one-page markdown summary — claims vs mechanically verified facts vs findings
raised this session — to .codecouncil/receipts/ for a human to read.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.store import read_tail_rows

CLAIM_VERB_RE = re.compile(r"\b(add|fix|implement|handle|test|complete|done|pass)\w*\b",
                           re.IGNORECASE)
MAX_CLAIM_BULLETS = 6
CLAIM_TRUNCATE_CHARS = 160
FILES_CHANGED_RE = re.compile(r"(\d+)\s+files?\s+changed")
RECEIPTS_KEEP = 50
SLUG_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Task 2: test-integrity verdict (strengthened/unchanged/weakened) embedded
# in every receipt so the done-gate (hooks/logic.py) can read it back without
# re-parsing the session's diffs. Default matches screen._UNCHANGED_INTEGRITY
# so a caller that doesn't pass test_integrity (old call sites, tests) keeps
# rendering an inert "unchanged" block rather than crashing.
_UNCHANGED_TEST_INTEGRITY = {"verdict": "unchanged", "tests_added": 0, "tests_removed": 0,
                             "asserts_added": 0, "asserts_removed": 0}
TEST_INTEGRITY_FENCE = "test-integrity"
_TEST_INTEGRITY_BLOCK_RE = re.compile(
    r"```" + TEST_INTEGRITY_FENCE + r"\n(.*?)\n```", re.DOTALL)


def _test_integrity_line(ti: dict) -> str:
    verdict = ti.get("verdict", "unchanged")
    if verdict == "weakened":
        detail = (f"{ti.get('asserts_removed', 0)} assertion(s) removed, "
                  f"{ti.get('asserts_added', 0)} added")
        if ti.get("tests_removed"):
            detail = f"{ti['tests_removed']} test(s) removed, " + detail
        return f"weakened — {detail} (details in screening signals)"
    if verdict == "strengthened":
        parts = []
        if ti.get("tests_added"):
            parts.append(f"{ti['tests_added']} test(s) added")
        if ti.get("asserts_added"):
            parts.append(f"{ti['asserts_added']} assertion(s) added")
        return f"strengthened — {', '.join(parts) or 'tests strengthened'}"
    return "unchanged"


def parse_test_integrity(receipt_text: str) -> dict | None:
    """Pull the test_integrity JSON block back out of a rendered receipt
    (used by hooks/peer_hook.py, which reads receipt files but must hand
    decide() already-parsed data — decide() itself stays fs-free). None if
    the block is missing (a receipt written before this feature) or
    unparseable — the done-gate treats that as "nothing to block on"."""
    m = _TEST_INTEGRITY_BLOCK_RE.search(receipt_text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _truncate(text: str, limit: int = CLAIM_TRUNCATE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _claims(events: list[dict]) -> list[str]:
    """Commit subjects (what actually landed) plus recent reasoning that reads
    like a completion claim, capped to MAX_CLAIM_BULLETS and truncated."""
    subjects: list[str] = []
    for e in events:
        if e.get("type") == "commit":
            subjects.extend(e.get("payload", {}).get("subjects", []))
    claimy_reasoning = [
        e["payload"]["text"]
        for e in events
        if e.get("type") == "reasoning"
        and CLAIM_VERB_RE.search(e.get("payload", {}).get("text", "") or "")
    ]
    bullets = (subjects + claimy_reasoning)[-MAX_CLAIM_BULLETS:]
    return [_truncate(b) for b in bullets]


def _files_changed(events: list[dict]) -> int | None:
    """File count from the latest diff event's `git diff --stat` summary line."""
    diffs = [e for e in events if e.get("type") == "diff"]
    if not diffs:
        return None
    stat = (diffs[-1].get("payload") or {}).get("stat", "") or ""
    m = FILES_CHANGED_RE.search(stat)
    return int(m.group(1)) if m else None


def _findings(suggestions_file: Path, since_epoch: float, now_epoch: float) -> list[dict]:
    """Every SUGGESTION verdict raised within [since_epoch, now_epoch], joined
    with its outcome grade (or "pending" if the Reflector hasn't graded it
    yet). Bounded tail reads — same shape as critic.main.verdict_history."""
    outcomes_file = suggestions_file.parent / "outcomes.ndjsonl"
    grades = {o.get("suggestion_id"): o.get("outcome") for o in read_tail_rows(outcomes_file)}
    out = []
    for row in read_tail_rows(suggestions_file):
        if row.get("verdict") != "SUGGESTION":
            continue
        try:
            ts = datetime.fromisoformat(row["ts"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if not (since_epoch <= ts <= now_epoch):
            continue
        s = row.get("suggestion") or {}
        out.append({
            "severity": s.get("severity", "?"),
            "file": s.get("file", "?"),
            "issue": s.get("issue", "?"),
            "verification": (row.get("verification") or {}).get("status", "unverified"),
            "outcome": grades.get(row.get("id"), "pending"),
        })
    return out


def _verdict_line(review_record: dict) -> str:
    verdict = review_record.get("verdict", "?")
    if verdict == "SUGGESTION":
        s = review_record.get("suggestion") or {}
        return f"ISSUE — {s.get('file', '?')}: {s.get('issue', '?')}"
    if verdict == "PASS":
        reason = review_record.get("reason")
        return f"PASS — {reason}" if reason else "PASS"
    return str(verdict)


def _slug(events: list[dict], repo_name: str) -> str:
    session = next((e.get("session") for e in events if e.get("session")), None)
    raw = session or repo_name or "repo"
    slug = SLUG_SANITIZE_RE.sub("-", raw).strip("-")
    return slug or "repo"


def write_receipt(cc: Path, ctx_like: dict, events: list[dict], review_record: dict,
                  tests_fact: str, test_integrity: dict | None = None) -> Path:
    """Render .codecouncil/receipts/<session-or-repo>-<YYYYmmdd-HHMMSS>.md — a
    one-page claims-vs-verified summary for a human — and prune the directory
    to the newest RECEIPTS_KEEP files.

    ctx_like carries the same keys as the critic's ctx dict: "repo" (for the
    header/slug), "suggestions_file" (for the findings section), and
    "since_epoch" (the review window's start, for scoping findings).

    test_integrity is screen.test_integrity()'s output for this session's
    diffs (critic.main computes it from the same batch diffs judge_batch
    screens). Defaults to an inert "unchanged"/zeros block so callers that
    don't pass it (and existing receipts before this feature) render the
    same shape rather than omitting the section or crashing.
    """
    repo = ctx_like.get("repo")
    repo_name = Path(repo).name if repo else "repo"
    now = datetime.now(timezone.utc)

    lines = [
        f"# CodeCouncil Session Receipt — {repo_name}",
        "",
        f"- Generated: {now.isoformat()}",
        f"- Verdict: {_verdict_line(review_record)}",
        "",
        "## Claimed",
    ]
    claims = _claims(events)
    lines += [f"- {c}" for c in claims] if claims else ["(no claims captured this window)"]
    lines.append("")

    lines.append("## Mechanically verified")
    lines.append(f"- {tests_fact}")
    n_files = _files_changed(events)
    lines.append(
        f"- files changed (latest diff): {n_files if n_files is not None else 'none captured in this window'}"
    )
    lines.append("")

    ti = test_integrity if test_integrity is not None else dict(_UNCHANGED_TEST_INTEGRITY)
    lines.append("## Test integrity")
    lines.append(f"- tests: {_test_integrity_line(ti)}")
    lines.append(f"```{TEST_INTEGRITY_FENCE}")
    lines.append(json.dumps(ti, sort_keys=True))
    lines.append("```")
    lines.append("")

    lines.append("## Findings this session")
    suggestions_file = ctx_like.get("suggestions_file")
    findings = (
        _findings(suggestions_file, ctx_like.get("since_epoch", 0.0), now.timestamp())
        if suggestions_file else []
    )
    if findings:
        for f in findings:
            lines.append(
                f"- [{f['severity']}] {f['file']} — {f['issue']} "
                f"(verification: {f['verification']}, outcome: {f['outcome']})"
            )
    else:
        lines.append("(none)")
    lines.append("")

    lines.append(f"Generated by CodeCouncil · heuristics v{review_record.get('heuristics_version', '?')}")

    receipts_dir = cc / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    path = receipts_dir / f"{_slug(events, repo_name)}-{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from .main import _prune_dir  # local import: critic.main imports this module at load time
    _prune_dir(receipts_dir, "*.md", RECEIPTS_KEEP)

    return path
