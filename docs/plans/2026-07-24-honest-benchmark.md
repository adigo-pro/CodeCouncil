# Honest Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one honest, reproducible headline table for CodeCouncil — modeled on ponytail's agentic benchmark methodology — that measures what a *reviewer* can truthfully claim: safety (vulnerabilities blocked), correctness (edge-case bugs caught), integrity (false claims caught), and the cost it *adds*.

**Architecture:** Extend the existing `evals/ab/` harness with the four methodology upgrades ponytail's benchmark demonstrates: (1) control arms that isolate the treatment's specific value, (2) a safety tier scored by executing adversarial input, (3) contamination-proof arm isolation, (4) a no-spend scorer self-test. Then a real-OSS-repo substrate option. Every number ships with its raw rows and a limitations section. Python stays stdlib-only (the `claude` CLI is the harness, as in ponytail).

**Tech Stack:** stdlib Python 3.10+; the `claude -p --output-format json` CLI (already the A/B harness's engine); existing `evals/ab/{run,score,tasks}.py`, `critic/screen.py`, `critic/verify.py`.

**Attribution:** methodology adapted from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)'s `benchmarks/agentic/` (MIT) — the multi-arm control design, execute-the-exploit safety tier, per-arm isolation, and `--selftest` gate. Credit it in the writeup; that's the open-source deal.

## Global Constraints

- Runtime + harness stay stdlib-only; no pip deps. The `claude` CLI carries cost/tokens/duration in its JSON output — read those, don't add an SDK.
- **Honesty is the product.** No metric CodeCouncil cannot truthfully own. A reviewer ADDS cost/latency — report that as a positive number, never hide or spin it. No claim of "cheaper/faster."
- Every scorer must be **discriminating**: it passes a correct reference AND fails a planted-bad reference, proven by `--selftest` with zero API spend, before any live run.
- Arm isolation is load-bearing: the without-council arm must run a `claude` session that CANNOT inherit council hooks (ponytail's contamination bug — a global hook fired on their baseline). Prove isolation in a test.
- No live multi-session experiment runs inside a task (each costs real API $); tasks make the harness ready + self-tested. Running it is a maintainer decision, documented.
- Suite green at every task boundary (currently 544); README count updated in the same commit when it changes.
- `.codecouncil/` in this repo is live data — never write into it.

## Ponytail lessons → tasks

| Lesson from their benchmark | Our task |
|---|---|
| Multi-arm with controls (baseline · treatment · terse-control · naive-prompt) isolates *what* helps | T1: naive-control arm |
| Safety tier: execute produced code against adversarial input, report safe-rate | T2: safety tier |
| Contamination bug: a global hook fired on the baseline arm, secretly running the treatment | T3: arm isolation + `--setting-sources` guard |
| `--selftest` verifies every scorer catches bad / passes good with no API spend | T4: scorer self-test gate |
| Real, pinned OSS repo as substrate (not synthetic) makes numbers credible | T5: real-repo substrate (opt-in) |
| Honest writeup: walk back inflated numbers, limitations section, raw rows | T6: honest headline writeup + site |

---

### Task 1: Naive-control arm — isolate CodeCouncil's *specific* value

Ponytail added `caveman` (terse prose) and `yagni-oneliner` (a 7-word prompt) to prove their skill beats cheap alternatives. Our equivalent: a `naive-review` arm where the agent just gets a generic *"review your own code for bugs and security issues before finishing"* line appended to its prompt — no council. If CodeCouncil's verified, executed review doesn't beat self-nagging, we need to know.

**Files:**
- Modify: `evals/ab/run.py` (arm dimension: `without` · `naive` · `with`), `evals/ab/score.py` (report the third arm)
- Test: `tests/test_ab.py`

**Interfaces:**
- Produces: `--arms` accepts `without,naive,with` (comma list) or `all`; `naive` runs a bare `claude` session (isolated like `without`) but appends `NAIVE_REVIEW_PROMPT` (module constant) to the instruction. No council daemons, no hooks.
- Consumes: existing `run_session` (add an optional `append: str` param threaded to the `claude -p` invocation — the CLI's system-prompt-append flag, or prepended to the instruction; pick what the installed CLI supports and document it).

- [ ] **Step 1: Failing test** — `build_parser().parse_args(["--arms","without,naive,with"]).arms == ["without","naive","with"]`; `run_trial(..., arm="naive")` calls `run_session` with the naive append and starts NO daemons (mock Popen, assert 0 council spawns).
- [ ] **Step 2: Implement** the arm plumbing + `NAIVE_REVIEW_PROMPT`. The naive arm's isolation is identical to `without` (Task 3 hardens both).
- [ ] **Step 3: Report** the third column in `report()` (mean per arm, as today).
- [ ] **Step 4: Suite green, commit.**

---

### Task 2: Safety tier — execute the exploit, report a safe-rate

Ponytail's Axis 2: seed a starter file, ask for one function with the safety requirement left *implicit* (as a real ticket reads), then execute the produced function against adversarial input and report safe-rate per arm. This is CodeCouncil's whole thesis measured head-on. Reuse the T1-failure-mode eval discipline and the A/B hidden-test machinery.

**Files:**
- Create: `evals/ab/safety_tasks.py` (5 surgical tasks, each: starter seed, implicit-safety instruction, adversarial hidden test that EXECUTES the produced function)
- Modify: `evals/ab/run.py` (a `--tier safety|feature|both` selector; safety trials score safe/unsafe, not LOC), `evals/ab/score.py` (safe-rate aggregation)
- Test: `tests/test_ab.py`, `tests/test_ab_safety.py`

**Interfaces:**
- Produces: each safety task is `(name, seed_files: dict, instruction, adversarial_test)`. The adversarial test imports the produced module and runs the exploit (path traversal `../../etc`, SQL `' OR '1'='1`, a forged token, a malformed row, a quota-exhausting input) — prints `SAFE`/`UNSAFE`, exits 0 only if safe. `score.safe_rate(rows)` → per-arm `n_safe/n_total`.
- Fairness rule (copy verbatim into the module docstring): the adversarial test asserts ONLY the implicit safety property the instruction's domain requires; the `bad` reference is the lazy-but-plausible version (happy-path-correct, adversarial-unsafe). Both references live beside each task for `--selftest`.

- [ ] **Step 1: Write 5 safety tasks** with a `good` and `bad` reference impl each (the references power the self-test in T4). Domains: path-join, SQL lookup, token check, CSV parse, rate limit — mirror ponytail's set but write our own.
- [ ] **Step 2: Failing test** — each adversarial test passes its `good` reference and fails its `bad` reference (this IS the discrimination proof; run references directly, no API).
- [ ] **Step 3: Implement** the tier selector + safe-rate scoring.
- [ ] **Step 4: Suite green, commit.**

---

### Task 3: Contamination-proof arm isolation

Ponytail's most important finding: their `SessionStart` hook fired on *every* arm including the baseline, so the baseline was secretly running the treatment — a ~4% false result they nearly published. CodeCouncil installs hooks to the repo-local `.claude/settings.json` and the without-arm never calls `install_hooks`, so we're isolated at the daemon level — BUT `run_session` calls `claude -p` without excluding the user's *global* settings, so a maintainer reproducing our benchmark on a machine with global council hooks would silently contaminate `without`/`naive`. Close it the way ponytail did.

**Files:**
- Modify: `evals/ab/run.py` (`run_session` passes `--setting-sources project,local` — or the installed CLI's equivalent for excluding user/global settings — on the isolated arms; the `with` arm uses project settings where hooks are installed)
- Test: `tests/test_ab.py` (assert the isolation flag is present on without/naive `claude` argv, and that a marker hook placed in a fake global settings dir does NOT reach an isolated-arm session — a contained integration test using a temp HOME)

**Interfaces:**
- Produces: isolated arms invoke `claude` such that no user/global hook can fire. Verify the installed `claude --help` for the exact flag; if the flag name differs from ponytail's `--setting-sources`, use the real one and document it. If no such flag exists, fall back to running with a scrubbed `CLAUDE_*`/`HOME` env and document that instead — the invariant (no global hook reaches an isolated arm) is what's tested, not the specific mechanism.

- [ ] **Step 1: Failing test** — plant a marker hook in a temp global settings dir, point the session's HOME/settings at it, run an isolated-arm invocation (mock the actual `claude` call, assert the argv/env would exclude it) — assert the isolation mechanism is applied.
- [ ] **Step 2: Implement** the isolation flag/env on `without`/`naive`.
- [ ] **Step 3: Document** the contamination risk + fix in `evals/ab/run.py`'s module docstring, crediting the ponytail finding.
- [ ] **Step 4: Suite green, commit.**

---

### Task 4: `--selftest` — prove every scorer discriminates, no API spend

Ponytail runs `python run.py --selftest` first, always: every scorer must catch its `bad` reference and pass its `good` reference with zero API. This is what stops a broken scorer from silently poisoning a $40 run. We have `rescore`; add the self-test gate.

**Files:**
- Modify: `evals/ab/run.py` (add `--selftest` that runs each safety task's adversarial test against its `good` (must pass) and `bad` (must fail) references, and each feature task's hidden test against a known-good reference where one exists; prints a table, exits nonzero if any scorer fails to discriminate)
- Test: `tests/test_ab.py` (assert `--selftest` returns 0 on the shipped references and would return nonzero if a scorer were non-discriminating — inject a bad scorer in the test)

**Interfaces:**
- Produces: `evals.ab.run --selftest` → per-scorer `DISCRIMINATES / BROKEN` lines, exit 0 iff all discriminate. Zero `claude` calls (mock/skip the session entirely — this only runs scorers against reference code).

- [ ] **Step 1: Failing test** — `--selftest` exits 0 on shipped references; monkeypatch one safety task's `good`==`bad` and assert it exits nonzero (a scorer that can't tell good from bad is BROKEN).
- [ ] **Step 2: Implement** the self-test loop.
- [ ] **Step 3: Wire into CI** as a fast, no-API gate (`.github/workflows/ci.yml` — a `bench-selftest` job running `python3 -m evals.ab.run --selftest`), so a scorer regression fails CI, not a live run.
- [ ] **Step 4: Suite green, commit.**

---

### Task 5: Real-OSS-repo substrate (opt-in)

Ponytail runs against a pinned real repo (`tiangolo/full-stack-fastapi-template`) for credibility. Add an opt-in substrate so feature-tier trials can run against a real cloned+pinned repo instead of the synthetic seed, without making the default depend on a network clone.

**Files:**
- Modify: `evals/ab/run.py` (`--repo-url URL@sha` option: when set, each trial's workspace is a fresh clone pinned to that SHA instead of `seed_repo`'s synthetic files; default unchanged — synthetic, offline, hermetic)
- Test: `tests/test_ab.py` (the option parses; when unset, `seed_repo` is used — no network in the default test path)

**Interfaces:**
- Produces: `--repo-url` optional; absent → today's synthetic seed (tests stay offline). Present → `git clone --depth 1` + `git checkout <sha>` per workspace. Document that this is the credible-numbers path and needs network + the tasks written against that repo's tickets.

- [ ] **Step 1: Failing test** — parser accepts `--repo-url`; `run_trial` with it unset calls `seed_repo` (mock, assert), with it set would clone (assert the clone command is constructed, mock subprocess — no real network in the test).
- [ ] **Step 2: Implement** the substrate switch.
- [ ] **Step 3: Suite green, commit.**

---

### Task 6: The honest headline — writeup + site

Turn the harness into a claim. Write the methodology + limitations doc (ponytail's `2026-06-18-agentic.md` is the template: restate the honest critique, name what's controlled for, publish raw rows, list limitations "so this can't be the next thing someone debunks"). Then one headline table on the site — safety-rate and catch-rate, with the added-cost column shown openly.

**Files:**
- Create: `docs/benchmarks/METHODOLOGY.md` (the reproducible method, arms, metrics, isolation, self-test, limitations, ponytail attribution)
- Modify: `codecouncil-web` benchmarks page (a headline table placeholder wired to say "run it yourself" until real numbers exist — NO fabricated numbers; the table shows the *metrics* and the reproduce command, with any filled cells sourced only from a committed results file)
- Test: n/a (docs) — but a test that METHODOLOGY.md's reproduce command matches the actual CLI (`tests/test_ab.py` asserts the documented flags parse)

**Interfaces:**
- Produces: a methodology doc anyone can run; a site section that promises measurement, not marketing. Until a real multi-session run is done (maintainer decision), the site says "reproduce: `python3 -m evals.ab.run --tier both --arms all --trials 4`" and shows the metric columns empty-but-defined — honest emptiness over invented numbers.

- [ ] **Step 1: Write METHODOLOGY.md** — arms, tiers, isolation, self-test, limitations, attribution.
- [ ] **Step 2: Site table** — metrics + reproduce command; a `results.ndjsonl`-driven fill when one exists, empty otherwise.
- [ ] **Step 3: Test** the documented flags parse (`build_parser().parse_args(<documented argv>)` succeeds).
- [ ] **Step 4: Suite green, commit.** (Site deploy is a separate maintainer step.)

---

## Self-review notes

- **Coverage:** each ponytail lesson maps to a task (control→T1, safety-tier→T2, isolation→T3, selftest→T4, real-repo→T5, honest-writeup→T6).
- **Honesty guardrails baked in:** no efficiency claims (Global Constraints); no fabricated site numbers (T6); scorers must discriminate before spending (T4); isolation proven, not assumed (T3).
- **Order:** control + tier + isolation are the measurement foundation (T1–T3); the self-test gate protects them (T4); substrate + writeup make it credible and public (T5–T6). Each independently shippable and suite-green.
- **Deliberately out of scope:** running the live multi-session experiment (maintainer cost decision — the harness is the deliverable, the numbers come from running it); multi-model sweeps (the harness supports it; we validate on one); JS/other-agent portability (separate adapters backlog).
- **Attribution is non-negotiable:** METHODOLOGY.md credits ponytail's benchmark design explicitly. Borrowing openly and saying so is the community deal the user named.
