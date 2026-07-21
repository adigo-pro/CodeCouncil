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
#
# Deliberately global, not per-watched-repo: every repo CodeCouncil watches
# harvests into this one shared directory, so a finding graded in repo A
# strengthens the eval gate (and therefore the heuristics) used while
# watching repo B too. Cross-repo learning is the point, not an accident —
# don't scope this path by repo.
HARVESTED_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases-harvested"

MAX_HARVESTED = 40


def _content_hash(file: str, issue: str, extra: str = "") -> str:
    return hashlib.sha1(f"{file}|{issue}|{extra}".encode("utf-8")).hexdigest()[:12]


def _prune_dir(dir_path: Path, pattern: str, keep: int) -> None:
    """Cap a directory to its newest `keep` files (by mtime), oldest evicted
    first. Mirrors critic/main.py's _prune_dir (prompts/, case-material/) so
    evals/cases-harvested/ grows without limit either — it caps and rolls
    over instead. evals/cases/ (hand-made) is a sibling, separate directory
    and is never touched by this."""
    files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep]:
        old.unlink(missing_ok=True)


def _load_material(cc: Path, sid: str) -> dict | None:
    material_path = cc / "case-material" / f"{sid}.json"
    if not material_path.exists():
        return None
    try:
        return json.loads(material_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_case(sid: str, hash_file: str, file_name: str, issue: str,
                expected: str, material: dict, dedupe_extra: str = "") -> str | None:
    """Shared dedupe + cap + write tail, used by both the SUGGESTION-graded
    path and the missed-PASS path below. `hash_file` is the raw (possibly
    directory-qualified) file identifier the dedupe hash is keyed on;
    `file_name` is the basename that actually goes into expect_files.
    `dedupe_extra` folds additional context into the dedupe hash — the
    missed-PASS path uses it for the fix commit's subject, so two distinct
    misses on the same file (different fix commits) don't collapse into one
    harvested case."""
    content_hash = _content_hash(hash_file, issue, dedupe_extra)

    HARVESTED_DIR.mkdir(parents=True, exist_ok=True)
    existing_files = sorted(HARVESTED_DIR.glob("*.json"))
    for existing in existing_files:
        try:
            existing_obj = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if existing_obj.get("_content_hash") == content_hash:
            return None  # dedupe: same flagged file+issue already harvested

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
    # Cap the directory to the newest MAX_HARVESTED cases, oldest evicted
    # first — mirrors critic/main.py's _prune_dir pattern (prompts/,
    # case-material/) rather than refusing to harvest once full. Runs after
    # the write so the just-written case is itself eligible to count toward
    # (and, on a future call, be evicted alongside) the cap. evals/cases/
    # (hand-made) is a separate directory and is never touched.
    _prune_dir(HARVESTED_DIR, "*.json", MAX_HARVESTED)
    return name


def maybe_harvest(cc: Path, suggestion_row: dict, outcome: str,
                  miss_file: str | None = None,
                  commit_subject: str | None = None) -> str | None:
    """Freeze a graded suggestion's judged inputs into a new eval case, if it
    qualifies. Returns the case name written, or None.

    Rules:
    - accepted + verification verified (or no verification) -> must-FLAG case.
    - verification refuted (regardless of outcome), OR rebutted with the
      flagged file untouched -> must-PASS case. A rebuttal where the file WAS
      touched is ambiguous (the agent may have fixed the issue while
      disagreeing about something else) so it is not harvested either way.
    - missed (a PASS verdict later contradicted by a fix commit touching a
      reviewed file, see reflector.misses) -> must-FLAG case. `suggestion_row`
      here is the PASS row itself, which has no "suggestion" dict to read a
      file from, so the flagged file comes from `miss_file` instead.
      `commit_subject` (the fix commit's subject, from reflector.misses'
      output) is folded into the dedupe hash so two distinct misses on the
      same file — different fix commits — harvest as two cases instead of
      collapsing into one under the old (file, "") hash.
    """
    if outcome == "missed":
        if not miss_file:
            return None
        sid = suggestion_row.get("id")
        if not sid:
            return None
        material = _load_material(cc, sid)
        if material is None:
            return None
        file_name = Path(miss_file).name
        issue = suggestion_row.get("reason", "")
        return _write_case(sid, miss_file, file_name, issue, "flag", material,
                           dedupe_extra=commit_subject or "")

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
    material = _load_material(cc, sid)
    if material is None:
        return None

    raw_file = suggestion.get("file", "")
    file_name = Path(raw_file).name
    issue = suggestion.get("issue", "")
    return _write_case(sid, raw_file, file_name, issue, expected, material)
