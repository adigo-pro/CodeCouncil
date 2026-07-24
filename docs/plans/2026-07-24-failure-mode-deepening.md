# Failure-Mode Deepening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn v1's attention-direction screening into proof-generating review for the four research-documented AI-code failure modes: insecure-by-default (45%), almost-right logic (1.75×), hallucinated dependencies, and verification gaming.

**Architecture:** Every mechanism exploits an axiom competitors lack: the executor (staging-dir repros), intent (transcripts + docstrings beside diffs), in-session delivery, and the measured loop. Each detector lands in the same task as its frozen eval slice — measurement precedes mechanism (the A/B pilot's lesson). All Python stays stdlib-only.

**Tech Stack:** stdlib Python 3.10+; existing seams: `critic/screen.py` (signals), `critic/verify.py` (staging executor), `critic/receipt.py` (claims-vs-verified), `hooks/logic.py` (pure delivery decisions), `evals/cases/` (frozen slices).

## Global Constraints

- Runtime stdlib-only; no new pip dependencies anywhere in the loops.
- `hooks/peer_hook.py` fails open; `hooks/logic.py` and `codecouncil/signal_filter.py` stay pure (no I/O).
- Eval hermeticity: no-signal / no-feature paths must produce byte-identical prompts and records to today (the council-mode precedent).
- Every model-call addition is budgeted and floored: probes and exploit turns must respect the existing per-beat model-call floor; new calls are capped by module-level constants.
- Precision first: a new detector ships only with must-NOT-flag eval cases alongside its must-flag cases.
- Suite green at every task boundary (`python3 -m unittest discover -s tests`); README count updated in the same commit whenever it changes (currently 432).
- Repro/exploit code executes ONLY in `verify.py`-style throwaway staging dirs, never the watched repo.

---

### Task 1: Frozen eval slices for all four failure modes

Measurement first. Eight hand-made frozen cases (two per failure mode: one must-flag, one must-pass) so every later detector — and every future heuristics rewrite — is gated against these behaviors.

**Files:**
- Create: `evals/cases/security-sqli.json` (must-flag: f-string into execute), `evals/cases/security-param-clean.json` (must-pass: `%s` + params tuple), `evals/cases/logic-promise-gap.json` (must-flag: docstring promises None-on-invalid, code raises), `evals/cases/logic-edge-clean.json` (must-pass: correct boundary handling with tests), `evals/cases/dep-hallucinated.json` (must-flag: import of a nonexistent package), `evals/cases/dep-known-clean.json` (must-pass: adds a real stdlib import), `evals/cases/gaming-assert-removed.json` (must-flag: test assertions deleted alongside impl change), `evals/cases/gaming-test-refactor-clean.json` (must-pass: assertions moved, count preserved)
- Test: `tests/test_evals.py` (extend)

**Interfaces:**
- Consumes: the existing case schema (`name`, `expected`, `expect_files`, `events`, `latest_diff`) — copy the shape of `evals/cases/docstring-trap.json` exactly.
- Produces: 8 files the rewrite gate and `evals.run` load automatically (directory glob — no code change needed).

- [ ] **Step 1: Write the 8 cases.** Each `events` list holds one `commit` event whose `payload.diff` is a minimal unified diff exhibiting (or correctly avoiding) the failure mode; `expect_files` names the file the finding must cite. Keep every diff under 40 lines. The two `logic-*` cases reuse the seed-file style of `training.run.SEED_FILES` (docstring promise vs behavior).
- [ ] **Step 2: Add a schema test** in `tests/test_evals.py`:

```python
def test_failure_mode_slices_well_formed(self):
    for p in sorted(CASES_DIR.glob("*.json")):
        case = json.loads(p.read_text())
        self.assertIn(case["expected"], {"flag", "pass"})
        if case["expected"] == "flag":
            self.assertTrue(case["expect_files"])
```

- [ ] **Step 3: Replay sanity** — run `python3 -m evals.run /path/to/this/repo` once with the live model and record the baseline score per case in the commit message (a case the current critic can't catch is fine — that's the point of gating future work; a must-pass case the critic FLAGS is not fine, fix the case).
- [ ] **Step 4: Suite green, commit** (`Eval slices: the four documented failure modes become frozen gates`).

---

### Task 2: Test-integrity verdict in receipts + done-gate

The gaming failure mode, closed end-to-end: every session receipt states whether the session's tests were strengthened, unchanged, or weakened — and a weakened verdict blocks "done" once, exactly like a high-severity finding.

**Files:**
- Modify: `critic/screen.py` (add `test_integrity(diff_text) -> dict`), `critic/receipt.py` (render the verdict), `hooks/logic.py` (gate), `critic/main.py` (pass batch diffs into receipt writing)
- Test: `tests/test_screen.py`, `tests/test_hooks.py`, `tests/test_critic_receipts.py`

**Interfaces:**
- Produces: `screen.test_integrity(diff_text)` → `{"verdict": "strengthened"|"unchanged"|"weakened", "tests_added": int, "tests_removed": int, "asserts_added": int, "asserts_removed": int}` (pure — counts from `added_lines_by_file`/`removed_lines_by_file`). Receipt JSON/markdown gains a `test_integrity` block. `hooks/logic.py` treats `verdict == "weakened"` on the latest receipt as block-once material with message: *"this session weakened its tests (N assertions removed, M added) — restore them, or explain with COUNCIL-REBUTTAL"*.

- [ ] **Step 1: Failing tests first** — `test_integrity` verdict table (strengthened: +2 asserts −0; unchanged: refactor with equal counts; weakened: any `test-removed`/net-negative asserts); `hooks/logic` blocks once on weakened, never twice, never on rebutted.
- [ ] **Step 2: Implement `test_integrity`** as a thin aggregation over the existing `scan_test_weakening` counters (share the regexes — no duplication).
- [ ] **Step 3: Receipt wiring** — `receipt.py` accumulates the session's commit diffs (already flowing to the critic), renders: `tests: weakened — 3 assertions removed, 1 added (details in screening signals)`.
- [ ] **Step 4: Gate wiring in `hooks/logic.py`** — pure function extension; ledger key `"test_integrity"` mirrors the existing block-once pattern.
- [ ] **Step 5: Suite green, commit.**

---

### Task 3: Dependency provenance — typo-distance + new-dependency receipts

The dangerous hallucination is resolvable-but-wrong. Two additions, both mechanical and offline (no network — privacy invariant).

**Files:**
- Create: `critic/pkg_names.py` (top ~1000 PyPI package names as a frozen tuple, with generation note), `critic/deps.py`
- Modify: `critic/screen.py` (call deps checks), `critic/receipt.py` (dependencies section)
- Test: `tests/test_deps.py`

**Interfaces:**
- Produces: `deps.suspicious_imports(names: dict[str, str]) -> list[dict]` — signals for (a) imports within Damerau-Levenshtein distance 1 of a top-1000 name but not equal to it (`kind: "typo-suspect-import"`, evidence names the near-miss: `import requsts — 1 edit from 'requests'`), stdlib names exempt; (b) `deps.new_dependency_lines(diff_text)` — added lines in `requirements*.txt` / `pyproject.toml` / `package.json` → receipt section `dependencies added this session: [...]` (claims-vs-verified for the supply chain).
- Consumes: `screen.new_import_names` (Task shipped in v1).

- [ ] **Step 1: Failing tests** — `requsts` flags with `requests` named; `requests` itself never flags; `numpyy` flags; stdlib `jsonn`… resolves to nothing known → falls through to the existing unresolvable-import check, not this one; requirements-line extraction on a sample diff.
- [ ] **Step 2: Damerau-Levenshtein ≤1** — implement the O(len) early-exit check directly (≤1 edit is decidable without the full DP table); frozen name list in `pkg_names.py` with a comment documenting the source snapshot and date.
- [ ] **Step 3: Wire into `screen.screen()`** (order: typo-suspect before unresolvable — a typo that happens to be installed is still suspect) and receipts.
- [ ] **Step 4: Eval alignment** — extend `evals/cases/dep-hallucinated.json`'s diff so the typo path is exercised by the frozen slice.
- [ ] **Step 5: Suite green, commit.**

---

### Task 4: Proof-by-exploit verification for security signals

The differentiator: SAST emits warnings; CodeCouncil executes the exploit. When the judge confirms a security signal, the verifier doesn't just re-read the code — it stages the file and runs a class-specific micro-exploit demonstrating the vulnerability.

**Files:**
- Modify: `critic/verify.py` (exploit templates per CWE), `critic/main.py` (route security-class confirmed findings through exploit verification)
- Test: `tests/test_verify.py`

**Interfaces:**
- Consumes: confirmed findings whose originating signal has `cwe in {"CWE-89","CWE-78","CWE-95","CWE-502"}` (thread the signal's `kind`/`cwe` through the suggestion record — new optional field `"screen_signal"`; absent field = today's behavior, byte-identical records otherwise).
- Produces: `verify.exploit_templates[cwe]` — a prompt addendum instructing the verification turn to *demonstrate* the class (e.g. CWE-89: "craft an input that alters the query's structure — e.g. `1 OR 1=1` — and show the executed query string differs from the parameterized intent; a repro that merely re-reads the code is NOT verification"). Existing staging-dir mechanics unchanged; verified exploit output lands in the delivered finding as today's repro does.

- [ ] **Step 1: Failing tests** — with `CRITIC_CMD` stubbed: a security finding routes through the exploit addendum (assert the addendum text reaches the verification prompt); a non-security finding's verification prompt is byte-identical to today; a refuted exploit is not delivered.
- [ ] **Step 2: Implement templates + routing.** Keep templates short (≤6 lines each), one per CWE, module-level constants.
- [ ] **Step 3: Live smoke on this repo** — plant `cursor.execute(f"SELECT {x}")` in a scratch file, confirm the delivered finding carries an executed exploit, then revert the plant. Record the transcript excerpt in the commit message.
- [ ] **Step 4: Suite green, commit.**

---

### Task 5: Property probes — the almost-right detector

Generalize the docstring-trap catch: for each function the diff adds or changes that carries a promise (docstring, or the agent's stated intent for it), one budgeted model turn derives up to 3 edge probes; the staging executor runs them; divergence between promise and behavior becomes a finding with the failing probe as its repro.

**Files:**
- Create: `critic/probe.py`
- Modify: `critic/main.py` (probe pass after judgment, before delivery; budget gate), `critic/persona.md` (one paragraph: probes are promises tested, not style opinions)
- Test: `tests/test_probe.py`

**Interfaces:**
- Produces: `probe.candidates(diff_text) -> list[{"file", "qualname", "promise"}]` (pure: added `def` blocks with docstrings, parsed via `ast` from the post-diff file content already flowing as `touched_contents`); `probe.run_probes(candidate, repo, ask) -> finding | None` — one `TASK: PROBE` model turn returns up to `MAX_PROBES_PER_FUNC = 3` executable probe snippets; each runs via the existing staging mechanics; first divergence → finding `{"issue": "docstring promises X; probe shows Y", "repro": <probe>}`.
- Budget: `MAX_PROBE_CALLS_PER_BEAT = 2`, module constant; probe turns count against the existing model-call floor; zero candidates → zero calls → byte-identical behavior.
- Delivery: probe findings flow through the SAME verify-then-deliver path as judge findings (they arrive pre-verified by construction — the probe IS the repro — but refuted-on-rerun still kills delivery).

- [ ] **Step 1: Failing tests** — `candidates()` extracts changed functions with docstrings only; budget respected (5 candidates → 2 calls); a stubbed probe turn returning a diverging probe produces a finding whose repro is the probe; a probe that errors (rather than diverges) produces NO finding (broken probe ≠ broken code — precision first).
- [ ] **Step 2: Implement `probe.py`** (~120 lines: ast walk, prompt template, staging run via `verify`'s helpers).
- [ ] **Step 3: Wire into `critic/main.py`** behind `--probes/COUNCIL_PROBES` opt-in (default OFF for one release — the A/B harness measures it before it defaults on; the council-mode precedent).
- [ ] **Step 4: Live smoke** — re-plant the classic `parse_version` docstring lie in a scratch repo, confirm a probe finding with executed divergence.
- [ ] **Step 5: Suite green, commit.**

---

### Task 6: A/B round 2 measures all of it

The pilot's fix (longer tasks, eval-profile critic) plus one task per failure mode, so "does any of this help" stays an experiment, not a belief.

**Files:**
- Modify: `evals/ab/tasks.py` (4 new harder tasks: a small web endpoint touching SQL (security), a multi-function module with interacting edge cases (logic), a task whose natural solution needs a new dependency (deps), a slow-suite task that tempts test-skipping (gaming)), `evals/ab/run.py` (eval-profile critic flags `--interval 5 --turn-spacing 10`; `--probes` passthrough)
- Test: `tests/test_ab.py` (task schema test already covers new tasks automatically)

- [ ] **Step 1: Write the 4 tasks** with hidden tests probing exactly the failure mode (e.g. security task's hidden test attempts the injection).
- [ ] **Step 2: Eval-profile flags** in `start_council`.
- [ ] **Step 3: Suite green, commit.** Running round 2 (≥3 trials, ~40+ sessions) is a maintainer decision — cost gate, not code gate.

---

## Self-review notes

- **Coverage:** each failure mode gets mechanism + eval slice + A/B measurement (security: T1+T4+T6; logic: T1+T5+T6; deps: T1+T3+T6; gaming: T1+T2+T6).
- **Order rationale:** measurement (T1) before any mechanism; cheapest/pure first (T2, T3); executor-dependent after (T4, T5); experiment last (T6). Each task independently shippable.
- **Byte-identical guarantees named per task** — the no-feature path never changes existing records/prompts (T2 receipt block only when diffs present; T4 `screen_signal` optional; T5 opt-in flag).
- **Deliberately out of scope:** cross-session finding dedup (separate backlog item), JS/TS screening parity (Python first — where our transcripts and probes already work), any network-touching dependency checks (privacy invariant).
