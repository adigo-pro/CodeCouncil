# Counterpart Pair-Programmer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the critic a true counterpart to AI coding agents: aware of their documented failure family, hunting it in their visible reasoning, delivering findings an agent can act on instantly, and learning which decorrelated checks actually land.

**Architecture:** Four additive upgrades, no new daemons. (1) A counterpart model: the persona names the systematic failure modes of LLM coding agents and hunts them in the reasoning stream; findings carry a `failure_mode` tag through the grade pipeline (same plumbing pattern as rule attribution). (2) Verified findings carry a one-line repro command the receiving agent can run itself. (3) Substantial plan/design documents get reviewed for internal consistency against repo invariants (CLAUDE.md excerpt now rides in every prompt's project header). (4) Per-failure-mode acceptance analytics feed the rewrite prompt so self-improvement learns which decorrelated checks pay off.

**Tech Stack:** stdlib Python; pi headless turns; stdlib unittest + CRITIC_CMD stubs.

## Global Constraints

- Python stdlib-only; loops communicate only via `.codecouncil/` files; peer_hook fail-open; hooks/logic.py pure.
- NDJSON partial-line tolerance; bounded reads on hot paths; unbounded for dedup/metric completeness.
- Redaction-at-capture invariant; anything model-authored that gets persisted is redacted+capped at parse time (established in `parse_reply` for issue/rationale — new persisted model text follows the same rule).
- Daemons never die; new daemon-path calls guarded.
- Two-signal separation (eval gate vs in-the-wild acceptance) untouched.
- Suite green before every commit; commit messages imperative + why + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: The counterpart model — failure-mode-aware critique

**Files:**
- Modify: `critic/persona.md`, `critic/heuristics.seed.md`, `critic/prompt.py` (parse_reply), `reflector/main.py` (outcome rows carry failure_mode)
- Test: `tests/test_critic.py`, `tests/test_reflector.py`

**Interfaces:**
- Produces: `prompt.FAILURE_MODES = frozenset({"claim-drift", "self-test-bias", "rationalization", "scope-trim", "assumption", "error-suppression", "secret", "plan-inconsistency", "other"})`; suggestion JSON schema gains optional `"failure_mode": <one of FAILURE_MODES or null>`; parse_reply keeps valid values else None (legacy replies fine — same guard style as `rule`); graded outcome rows copy `failure_mode` from the suggestion (both grading paths; never on missed/undelivered).

**Design:** `critic/persona.md` gains a section (verbatim):

```
## Who you are reviewing

You are reviewing an AI coding agent (frequently Claude-family). You are a
differently-trained model — your independent judgment is the entire value of
this review. Never defer to the agent's stated confidence; it is confident by
default. Its REASONING is visible to you, and these documented failure
patterns show up there before they show up in code:

- claim-drift: prose claims outpace the diff ("handled X" with no X visible).
- self-test-bias: tests written to pass the current implementation — asserting
  buggy behavior as expected, weakening or deleting a failing test.
- rationalization: "this is fine because…" / "for simplicity I'll…" justifying
  a shortcut immediately before taking it. The justification IS the signal.
- scope-trim: silently narrowing the task, then declaring the full task done.
- assumption: "X already handles this" stated as fact with no evidence the
  agent checked (with your repo tools, YOU can check).
- error-suppression: a broad except/default-return added to make a symptom
  disappear rather than fixing its cause.

When you flag, set "failure_mode" to the pattern you saw (or "other").
```

The suggestion JSON protocol line in the persona adds `"failure_mode": <pattern name or null>`. `critic/heuristics.seed.md` gains two rules: *"A justification for a shortcut in the agent's reasoning ('for simplicity', 'this is fine because') immediately before the shortcut lands is worth reading twice — check the shortcut, not the justification."* and *"A new or changed test that asserts the current implementation's exact behavior deserves a check: does it encode intent, or encode the bug?"* (live evolved heuristics.md untouched).

- [ ] **Step 1: Failing tests** — parse_reply keeps `"failure_mode": "claim-drift"`, nulls `"nonsense"`, defaults absent→None (mirror the rule-validation tests); grade_pending copies failure_mode onto accepted/rebutted/ignored outcome rows and not onto missed/undelivered (extend the existing rule-carry tests' fixtures).
- [ ] **Step 2: RED.**  **Step 3: Implement (persona/seed edits verbatim; enum guard in parse_reply; one-line carry in reflector).**  **Step 4: Focused + full suite GREEN.**  **Step 5: Commit** `"Counterpart critique: name and hunt the coding-agent failure family"`.

---

### Task 2: Findings an agent can act on — repro commands

**Files:**
- Modify: `critic/verify.py` (prompt + parse), `hooks/logic.py` (_describe)
- Test: `tests/test_verify.py`, `tests/test_hooks.py`

**Interfaces:**
- Produces: `verification` dicts gain optional `"repro": str` (≤200 chars, redacted); `_describe` renders ` [verify yourself: <repro>]` when status is verified and repro present.

**Design:** `verify.build_prompt` asks, after the CONFIRMED line spec: *"If CONFIRMED, add a second line: `REPRO: <one shell command, runnable from the repo root, that demonstrates the problem>`."* `verify.parse` captures the last `^\[?REPRO\]?\s*[:—–-]\s*(.+)$` match (same bracket tolerance as status labels), stores `repro` passed through `core.redact.redact()` and capped at 200 with the standard marker. The staging path in the repro is the model's view — `critic/main.normalize_file` already maps staged paths for `file`; for the repro command, replace any staging-dir prefix with `.` (best-effort string replace of the staging dir path, done in `verify.verify_finding` where the staging path is in scope). `_describe` appends the verify-yourself hint only on the context channel text (both channels share _describe — fine, include for block too).

- [ ] **Step 1: Failing tests** — parse extracts repro (bracket + colon forms, capped, redacted: a repro containing `nvapi-<20+>` carries the marker); parse without repro → no key; _describe with verified+repro renders the hint, without repro doesn't; staging-path replacement (unit-test the helper with a fake staging prefix).
- [ ] **Step 2: RED.**  **Step 3: Implement.**  **Step 4: GREEN + full suite.**  **Step 5: Commit** `"Verified findings carry a repro the receiving agent can run"`.

---

### Task 3: Plan review — catch design bugs before implementation

**Files:**
- Modify: `critic/prompt.py` (plan detection + addendum), `critic/main.py` (CLAUDE.md excerpt in project header)
- Test: `tests/test_critic.py`

**Interfaces:**
- Produces: `prompt.is_plan_material(latest_diff) -> bool` — True when any `+++ b/` path in the diff (or untracked/touched file) ends in `.md` AND that file contributes ≥40 added lines (count `^+` lines in its hunk region; for untracked_contents, ≥40 lines of content); `PLAN_REVIEW_ADDENDUM` module constant appended to the prompt by build_prompt when true; `main.project_context` includes a `REPO INVARIANTS:` block — first `CLAUDE_MD_EXCERPT_CHARS = 1200` chars of the watched repo's CLAUDE.md when present.

**Design:** Addendum text (verbatim): *"PLAN/DESIGN DOCUMENT DETECTED in this change. Additionally judge the document itself: internal contradictions; interfaces referenced in one section but defined differently (or never) in another; steps that violate the REPO INVARIANTS in the project header. A design bug caught now is worth ten caught in code — but the one-issue discipline still applies. Use failure_mode \"plan-inconsistency\"."* Rationale (comment): agent workflows produce plan documents that later agents implement verbatim; a contradiction caught at plan time (e.g. a module placed on the wrong side of an import boundary) prevents entire tasks of rework — observed in this repo's own history.

- [ ] **Step 1: Failing tests** — is_plan_material: 40+-added-line .md in diff → True; small .md edit → False; 40+-line .py → False; untracked 40-line .md → True; addendum present in build_prompt output only when true; project_context contains CLAUDE.md excerpt capped at 1200 with truncation marker, absent cleanly when no CLAUDE.md.
- [ ] **Step 2: RED.**  **Step 3: Implement.**  **Step 4: GREEN + full suite.**  **Step 5: Commit** `"Plan review: judge design documents against repo invariants at plan time"`.

---

### Task 4: Learn which decorrelated checks land — per-mode analytics

**Files:**
- Modify: `reflector/report.py` (build_mode_rows + third CLI table), `reflector/rewrite.py` (per-mode graded stats in the rewrite prompt), `core/knowledge.py` (distill prompt may state agent-profile facts)
- Test: `tests/test_reflector.py`, `tests/test_knowledge.py`

**Interfaces:**
- Produces: `report.build_mode_rows(suggestions, outcomes) -> list[dict]` per (heuristics_version, failure_mode): suggested/accepted/rebutted/ignored, None mode → "?" bucket, printed as a third table when any mode data exists (mirror build_rule_rows exactly — same shape, keyed on failure_mode); `rewrite.build_prompt` gains mode-stat lines for the current version (label counts "graded", matching the rule-stats label convention) plus the sentence *"Modes with accepts are where your independent perspective pays — keep hunting them; modes with only rebuttals/ignores need sharper rules, not abandonment."*; `knowledge.build_distill_prompt` adds one sentence: *"If the rebuttal reveals a durable trait of this agent or repo (how it runs tests, what it considers in-scope), the fact may state that trait."*

- [ ] **Step 1: Failing tests** — build_mode_rows aggregation incl. "?" bucket (mirror the build_rule_rows tests); rewrite prompt contains mode lines + the sentence; distill prompt contains the trait sentence.
- [ ] **Step 2: RED.**  **Step 3: Implement.**  **Step 4: GREEN + full suite.**  **Step 5: Commit** `"Per-failure-mode acceptance: learn which decorrelated checks land"`.

---

## Deferred (explicitly)

- Counter-rebuttal round (verified finding rebutted → one bounded re-flag with repro receipts): needs live rebuttal-of-verified data first; the delivery caps exist for a reason.
- Dashboard mode/rule tables: CLI-first; the acceptance-curve mirror invariant only binds the curve.
- Multi-model council: separate plan, unchanged.

## Self-review notes

- Coverage: counterpart awareness → T1; agent-actionable delivery → T2; plan-time review → T3; learning which checks land → T4. ✓
- Type consistency: `failure_mode` flows suggestion → outcome → build_mode_rows keyed identically to `rule` → rewrite prompt; T2's `repro` lives inside the existing `verification` dict; T3's addendum keys off the same diff shapes T1/T11-era helpers parse. ✓
- No new persisted model text escapes redact/cap: `repro` is redacted+capped (T2); `failure_mode` is enum-validated (T1). ✓
