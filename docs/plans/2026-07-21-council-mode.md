# Council Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two decorrelated critics judge every batch — a precision anchor (Nemotron) and a recall prober (gpt-5-mini) — with an agreement-or-execution-proof filter, so the council catches what either misses while delivering only what survives both perspectives or a repro.

**Architecture (from measured data, docs/benchmarks/2026-07-21-*):** Nemotron: 0 false positives ever, 2/4 catches. gpt-5-mini: 4/4 catches, 2 false positives. Merge rule: primary's verdict flows as today; a prober-only finding is delivered ONLY if repro-verification confirms it (the anchor's implicit veto is overridden only by execution evidence — ground truth per the project's axioms). Council is opt-in via `COUNCIL_PROBER`; unset = exactly today's single-critic behavior. Prober calls share the primary's gating (code-changed batches only), so cost stays ~1¢ per judged batch.

**Tech Stack:** stdlib Python; pi headless; stdlib unittest + CRITIC_CMD stubs.

## Global Constraints

- Python stdlib-only; file-bus only; peer_hook fail-open; hooks/logic.py pure (no I/O).
- Model-authored persisted text: redacted/capped/enum-validated at parse (established pattern).
- Daemons never die; worker-thread code must not mutate process env (thread-safety).
- Two-signal separation untouched; eval scoring (`score_heuristics`) stays single-model/hermetic — the council is a runtime delivery mechanism, not a scoring change.
- Backward compatibility: `COUNCIL_PROBER` unset ⇒ byte-identical behavior to today (all existing tests pass unmodified).
- Suite green before every commit; commit messages imperative + why + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Thread-safe model override in `agent.ask` (+ stub model visibility)

**Files:**
- Modify: `critic/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- `agent.ask(prompt, system=None, tools=None, cwd=None, model=None)` — new optional `model`: when given, used instead of the `COUNCIL_MODEL`/NVIDIA-default resolution (worker threads must never mutate `os.environ`; this parameter is how the council selects the prober). `None` ⇒ existing resolution unchanged.
- CRITIC_CMD stub invocation becomes `$CRITIC_CMD <prompt-file> <resolved-model-or-empty>` — a second argv the stub MAY read to answer per-model (existing single-arg stubs ignore extra argv; zero breakage — state this in a comment).

**Steps:**
- [ ] **1: Failing tests** — PI_BIN-shim test: `ask(..., model="openrouter/openai/gpt-5-mini")` puts exactly that after `--model` (and COUNCIL_MODEL env, if set, is ignored); `model=None` keeps current resolution (existing test). Stub test: CRITIC_CMD stub receives the resolved model as argv[2] (write a stub that echoes its argv[2] into its reply; assert).
- [ ] **2: RED.**  **3: Implement (param + argv append).**  **4: GREEN + full suite.**
- [ ] **5: Commit** `"agent.ask takes an explicit model — worker threads must not mutate env"`.

---

### Task 2: The council merge in `judge_batch`

**Files:**
- Modify: `critic/main.py` (judge_batch, ask_with_retry, ctx plumbing), `critic/render.py` (render council info)
- Test: `tests/test_critic.py`

**Interfaces:**
- `ask_with_retry(text, ctx, model=None)` — passes model through to `agent.ask`.
- `ctx["prober"]: str | None` — prober model string (Task 4 wires the CLI/env; tests set it directly).
- Suggestion/verdict rows gain, when prober ran: `"council": {"prober_model": str, "prober_verdict": "PASS"|"SUGGESTION"|"ERROR", "agreement": "both"|"primary-only"|"prober-only"}`.
- Merge rules (implement as a pure function `merge_council(primary: dict, prober: dict) -> tuple[dict, dict]` returning (chosen_parsed_verdict, council_info) so it unit-tests without model calls):
  - primary SUGGESTION + prober SUGGESTION → primary's suggestion, agreement "both".
  - primary SUGGESTION + prober PASS/ERROR → primary's, agreement "primary-only".
  - primary PASS + prober SUGGESTION → prober's suggestion, agreement "prober-only".
  - primary PASS + prober PASS/ERROR → primary's PASS, agreement "both" when prober PASS else "primary-only".
- Verification policy in judge_batch: agreement "prober-only" ⇒ verification is MANDATORY (run even under `--no-verify`? No — respect `--no-verify` by SKIPPING the prober entirely when verification is disabled; a prober without a verifier is a false-positive machine, per the bake-off data — comment this) and the record keeps the suggestion regardless (delivery gating is Task 3's job; the row is honest history).
- Prober errors never block the primary flow (wrap prober call; on AgentError record prober_verdict "ERROR").

**Steps:**
- [ ] **1: Failing tests** — merge_council all six input combos (table above); judge_batch with ctx["prober"] set + two-reply stub (branch on argv[2]: primary model → PASS, prober model → suggestion JSON) yields a row with council.agreement "prober-only" and the prober's suggestion; prober AgentError → primary verdict survives with prober_verdict ERROR; ctx without prober → row has NO council key and stub sees only one call (count via stub side-file); `--no-verify` ctx (verify False) + prober set → prober not called (stub call-count 1).
- [ ] **2: RED.**  **3: Implement.**  **4: GREEN + full suite.**
- [ ] **5: Commit** `"Council merge: precision anchor + recall prober, one verdict out"`.

---

### Task 3: Delivery gate + grade attribution for prober-only findings

**Files:**
- Modify: `hooks/logic.py` (_pending), `reflector/main.py` (outcome rows carry agreement)
- Test: `tests/test_hooks.py`, `tests/test_reflector.py`

**Interfaces:**
- `hooks/logic._pending` new rule (comment: the anchor's veto is overridden only by execution proof): a row whose `council.agreement == "prober-only"` is deliverable ONLY when `verification.status == "verified"`. All other rows: unchanged rules. Missing/absent council key ⇒ unchanged (backward compatible).
- Graded outcome rows copy `"council_agreement": row.get("council", {}).get("agreement")` (both grading paths; not on missed/undelivered) — so future analytics can compute acceptance per agreement class for free.

**Steps:**
- [ ] **1: Failing tests** — hooks: prober-only + verified delivers on both channels; prober-only + inconclusive/absent verification does NOT deliver on either channel; "both"/"primary-only" rows deliver under today's rules regardless; no council key unchanged. reflector: outcome row carries council_agreement for graded, absent for missed/undelivered.
- [ ] **2: RED.**  **3: Implement.**  **4: GREEN + full suite.**
- [ ] **5: Commit** `"Prober-only findings deliver only with repro proof"`.

---

### Task 4: Config plumbing, docs, and a live smoke

**Files:**
- Modify: `critic/main.py` (`--prober` flag / `COUNCIL_PROBER` env → ctx), `codecouncil/main.py` (passthrough + preflight: warn when prober set but its provider key missing), `README.md` (Council mode section: the two measured profiles + merge rule + cost note ~1¢/judged batch), `CLAUDE.md` (architecture: council paragraph)
- Test: `tests/test_critic.py` (flag→ctx), `tests/test_codecouncil.py` (preflight warning)

**Interfaces:**
- Precedence: `--prober <model>` > `COUNCIL_PROBER` env > None. `codecouncil` launcher gains `--prober` passthrough to the critic subprocess.
- Preflight: prober starting with "openrouter/" and no OPENROUTER_API_KEY in `_local_env()` → warning (mirror existing preflight style).
- Live smoke (manual step by the controller after merge, not a unit test): one council beat on this repo with `COUNCIL_PROBER=openrouter/openai/gpt-5-mini`, confirming two model calls and a council field in the row.

**Steps:**
- [ ] **1: Failing tests** — flag precedence into ctx; preflight warning present/absent.
- [ ] **2: RED.**  **3: Implement + docs.**  **4: GREEN + full suite.**
- [ ] **5: Commit** `"Council mode config: --prober / COUNCIL_PROBER, preflighted and documented"`.

---

## Deferred

- Per-agreement acceptance table in report/dashboard (data accrues via council_agreement now; table when n>0 exists).
- Third councilor / vote weighting — YAGNI until the pair's live data says otherwise.

## Self-review notes

- Coverage: thread-safe model selection (T1) → merge (T2) → delivery/grade semantics (T3) → config+docs+smoke (T4). ✓
- Backward compat: every task's no-council path asserted byte-identical (no council key, single stub call, unchanged delivery). ✓
- Type consistency: `council` dict shape defined once in T2 and read by T3's gate + grade copy; `ask_with_retry(text, ctx, model=None)` signature consistent across T2 call sites. ✓
- The `--no-verify`+prober interaction is decided (skip prober) and commented — no silent false-positive machine. ✓
