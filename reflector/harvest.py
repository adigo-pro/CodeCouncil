"""Auto-harvest graded outcomes into frozen eval cases, so the eval set that
gates heuristics rewrites (evals/cases/*.json, rewrite.gate_candidate) grows
from real signal instead of staying frozen at the 7 hand-made cases forever.
Closes the loop: outcomes -> new cases -> gate future rewrites.

Case material (critic.main.save_case_material) is captured from the same
observation events the critic judges from. Every text-bearing event field —
diff content and untracked file contents (observer.gitwatch), reasoning text
and tool_call commands (observer.transcript) — is passed through
core.redact.redact() at observer capture time, before it ever lands in
observations.ndjsonl. So everything harvested here is already redacted.
evals/cases-harvested/ is therefore safe to version and must NOT be
gitignored.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Sibling of evals/cases, inside the CodeCouncil source tree itself (not the
# watched repo's .codecouncil/ dir) — harvested cases are versioned right
# alongside the hand-made ones so evals.run.load_cases and the rewrite gate
# see both. A module-level Path (not a function) so tests can monkeypatch it.
HARVESTED_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases-harvested"

MAX_HARVESTED = 40


def _content_hash(file: str, issue: str) -> str:
    return hashlib.sha1(f"{file}|{issue}".encode("utf-8")).hexdigest()[:12]


def maybe_harvest(cc: Path, suggestion_row: dict, outcome: str) -> str | None:
    """Freeze a graded suggestion's judged inputs into a new eval case, if it
    qualifies. Returns the case name written, or None.

    Rules:
    - accepted + verification verified (or no verification) -> must-FLAG case.
    - verification refuted (regardless of outcome), OR rebutted with the
      flagged file untouched -> must-PASS case. A rebuttal where the file WAS
      touched is ambiguous (the agent may have fixed the issue while
      disagreeing about something else) so it is not harvested either way.
    """
    if suggestion_row.get("verdict") != "SUGGESTION":
        return None
    suggestion = suggestion_row.get("suggestion") or {}
    verification = suggestion_row.get("verification") or {}
    status = verification.get("status")

    if outcome == "accepted" and status in (None, "verified"):
        expected = "flag"
    elif status == "refuted" or (
        outcome == "rebutted" and not suggestion_row.get("file_touched", True)
    ):
        expected = "pass"
    else:
        return None

    sid = suggestion_row.get("id")
    if not sid:
        return None
    material_path = cc / "case-material" / f"{sid}.json"
    if not material_path.exists():
        return None
    try:
        material = json.loads(material_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    file_name = Path(suggestion.get("file", "")).name
    issue = suggestion.get("issue", "")
    content_hash = _content_hash(suggestion.get("file", ""), issue)

    HARVESTED_DIR.mkdir(parents=True, exist_ok=True)
    existing_files = sorted(HARVESTED_DIR.glob("*.json"))
    for existing in existing_files:
        try:
            existing_obj = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if existing_obj.get("_content_hash") == content_hash:
            return None  # dedupe: same flagged file+issue already harvested

    if len(existing_files) >= MAX_HARVESTED:
        return None  # cap reached — never evict hand-made or harvested cases

    name = f"harvest-{sid}"
    case = {
        "name": name,
        "expected": expected,
        "expect_files": [file_name] if expected == "flag" else [],
        "events": material.get("events", []),
        "latest_diff": material.get("latest_diff"),
        "_content_hash": content_hash,
    }
    (HARVESTED_DIR / f"{name}.json").write_text(json.dumps(case, indent=1), encoding="utf-8")
    return name
