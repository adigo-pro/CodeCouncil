# Script-Based Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the verification chain's named weak link (run 3): the verification turn depends on the model *executing tools*, which the NVIDIA/pi backend frequently fumbles (emits the call as literal text → `inconclusive`), silently withholding true prober findings. Replace with the probe path's proven pattern: the model returns a **script**; the harness executes it in staging; the exit evidence decides.

**Architecture:** `verify.verify_finding` keeps its exact signature and result shape (`{status, note, repro}`) — every caller (judge path, prober gate, probes re-verify, delivery rules) is untouched. Inside, the tool-enabled pi turn is replaced by: (1) a no-tools turn that must output one self-contained Python repro script (fence-tolerant parsing, probe precedent); (2) direct execution in the existing staging dir with timeout + PYTHONPATH; (3) verdict from the script's printed `CONFIRMED:`/`REFUTED:` line — anything else (parse failure, crash, timeout, no marker) stays `inconclusive`. The delivered `repro` becomes the actually-executed script — strictly better evidence, and it closes T5's old "discarded repro" minor too.

**Tech Stack:** stdlib; seams `critic/verify.py`, `critic/agent.py` (ask without tools), `critic/probe.py` (`_strip_fences` / staging-exec helpers — REUSE, don't duplicate), `tests/test_verify.py`.

## Global Constraints

- `verify_finding`'s signature and result dict shape are FROZEN — no caller changes anywhere.
- Scripts execute ONLY in the throwaway staging dir (existing mkdtemp + rmtree discipline), with a hard timeout; never the watched repo.
- Precision rule preserved: `verified` requires an executed `CONFIRMED:` marker; ambiguity of any kind → `inconclusive`; an executed `REFUTED:` → `refuted`. A broken script is NOT a verdict (probe precedent).
- No duplicated fence-stripping/staging code — shared helpers with `critic/probe.py` (extract to module level or import; document the small cross-module reuse).
- `CRITIC_CMD` test stubs keep working: the verification turn is still one `agent.ask` call whose reply is now a script — existing multi-kind stubs branch on prompt markers; the new prompt keeps a distinctive marker (`TASK: VERIFY`).
- Suite green (currently 635); README count updated when it changes.

---

### Task 1: Script-based `verify_finding`

**Files:**
- Modify: `critic/verify.py` (the turn + execution + parsing), `critic/probe.py` (export the shared fence-strip/exec helpers if currently private)
- Test: `tests/test_verify.py`

**Interfaces:**
- Unchanged externally. Internally: `VERIFY_PROMPT` becomes a no-tools instruction — given the finding + staged file contents, output ONE self-contained Python script that: reproduces the claimed issue and prints exactly `CONFIRMED: <one-line evidence>`; or demonstrates the claim is false and prints exactly `REFUTED: <one-line evidence>`; prints nothing conclusive otherwise. Script runs with the staged file(s) present, `cwd=staging`, `PYTHONPATH=staging`, timeout (existing constant or `VERIFY_EXEC_TIMEOUT = 60`).
- Verdict mapping (binding): stdout contains `CONFIRMED:` (first marker wins) → `status="verified"`, `note`=marker line, `repro`=the executed script text. `REFUTED:` → `status="refuted"`. No marker / nonzero-exit-without-marker / timeout / unparseable reply → `inconclusive` with a diagnostic note. Both markers present → `inconclusive` (contradictory script).

- [ ] **Step 1: Failing tests** (CRITIC_CMD stub returns scripts): confirmed-marker script → verified with the script as repro; refuted-marker → refuted; raising script → inconclusive (NOT verified, NOT refuted); timeout script (sleep > cap, patch the cap small) → inconclusive; fenced reply (```python …```) still parses (fence-strip shared with probe); both-markers → inconclusive; existing callers' behavior via the delivery rule tests still green (prober-only + verified ships, inconclusive doesn't).
- [ ] **Step 2: Implement** — reuse probe's fence/exec helpers; keep `TASK: VERIFY` marker in the prompt for stubs.
- [ ] **Step 3: Update any test that asserted the old tool-based prompt/tooling** (VERIFY_TOOLS may become unused — delete it and its references rather than leaving dead config; if the exploit addenda from proof-by-exploit append to the prompt, keep them appended to the new script instruction — they translate directly: "the script must demonstrate the class").
- [ ] **Step 4: Full suite; README; ruff; commit.**

---

### Task 2: Live proof + run 4 (controller-run)

- [ ] Live smoke: scratch repo, plant the classic `order-totals`-style unguarded parse, run one real judge+verify cycle (real NVIDIA model), confirm `status="verified"` with an executed script — the thing run 3 couldn't do. 3 attempts; report honestly.
- [ ] `--selftest`, then run 4: `python3 -m evals.ab.run --tier safety --arms all --trials 3 --gate 90`.
- [ ] `docs/benchmarks/2026-07-25-safety-run4.md`: the metric that must move is **verified-rate on generated findings** (run 3: mostly inconclusive) and `order-totals` delivered>0; safe-rates reported as always. Site + forward links updated. Honest whatever it says.

## Self-review notes
- Biggest risk: models write scripts that "confirm" by asserting the claim rather than demonstrating it (the old verification-proves-wrong-predicate gap). Mitigation stays prompt-level here (demand the marker line carry executed evidence) — a deeper predicate-check is out of scope and tracked on the backlog; the writeup must not overclaim what a CONFIRMED marker proves.
- If run 4's order-totals still doesn't deliver, the next diagnosis target is the finding→delivery timing under the 90s gate with script-exec latency — measure from rows before concluding.
