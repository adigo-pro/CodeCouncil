# First-Principles Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CodeCouncil learn from every decision it makes — including its silences — and let it gather evidence before it speaks.

**Architecture:** Four upgrades to the existing loops, no new daemons. (a) The critic records what every verdict *covered* so the reflector can later detect that a PASS missed a bug (a fix-shaped commit revising recently-PASSed files) and grade it `missed`, harvesting the packet as a must-flag eval case — learning signal from the 86% of decisions that are currently unlearnable. (b) Judgment turns get read-only repo tools so the critic can check a suspicion before flagging (evidence starvation is the dominant error source in our dogfood data). (c) Rebuttals distill into a per-repo `knowledge.md` injected into every prompt, so the same rebuttal never recurs. (d) Findings cite the heuristic rule that motivated them; grades then attach to rules, making rewrites surgical and evidence-linked.

**Tech Stack:** Python 3.10+ stdlib only; pi (headless, via `critic/agent.py`); stdlib `unittest` with `CRITIC_CMD` stubs; React/Vite dashboard (`ui/`).

## Global Constraints

- Python stdlib-only: no pip dependencies in observer/critic/reflector/hooks/training/evals.
- Loops communicate only through NDJSON/JSON files in the watched repo's `.codecouncil/`.
- `hooks/peer_hook.py` stays fail-open; `hooks/logic.py` stays pure (no I/O).
- Every NDJSON reader tolerates a partial trailing line and skips unparseable lines.
- Bounded reads (`core.store.read_tail_rows`) on hot/recency paths; dedup sets and metric consumers stay unbounded.
- Redaction invariant: any new text field entering events must pass `core.redact.redact()` at capture. (This plan adds no new capture fields — `reviewed_files` is paths only, and paths are deliberately not redacted.)
- Daemons never die: every new call site in a daemon loop is guarded (`try/except Exception` + one warning line) unless failure is already absorbed upstream.
- All model calls stub via `CRITIC_CMD` in tests; stubs that must answer multiple prompt kinds branch on content markers (e.g. `TASK: DISTILL` vs `HEURISTICS (v`).
- Commit conventions: imperative subject + why-body, ending `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Full suite (`python3 -m unittest discover -s tests`) must pass before every commit; `cd ui && npx tsc --noEmit && npm run build` must be clean whenever `ui/` is touched.

---

### Task 1: Record what every verdict covered

The reflector cannot grade a PASS as a miss unless it knows which files that PASS reviewed. Record it on every verdict row, and save case material for PASS verdicts too (today only SUGGESTIONs get material — a missed PASS needs its packet to become an eval case).

**Files:**
- Modify: `critic/main.py` (judge_batch: add `reviewed_files`; save case material on every verdict)
- Test: `tests/test_critic.py`

**Interfaces:**
- Consumes: `judge_batch(events, ctx)` as-is; `save_case_material(cc_dir, verdict_id, events, latest_diff)` as-is (currently called only for SUGGESTION).
- Produces: every row in `suggestions.ndjsonl` gains `"reviewed_files": list[str]` (repo-relative paths, may be empty); `case-material/<id>.json` now exists for PASS rows as well (same shape, same 200-file cap). Task 2 consumes both.

**Design:** `reviewed_files` = sorted union of (a) keys of `latest_diff.payload.touched_contents`, (b) `latest_diff.payload.untracked`, (c) paths parsed from `+++ b/<path>` lines of `latest_diff.payload.diff` (reuse `observer.gitwatch._touched_paths` — import it; it already skips `/dev/null`). Empty list when `latest_diff` is None.

- [ ] **Step 1: Write failing tests** in `tests/test_critic.py` (class `TestReviewedFiles`, `TestHeartbeatWithStub` patterns):

```python
def test_every_verdict_records_reviewed_files(self):
    # stub replies PASS; diff event carries touched_contents + untracked
    self._set_stub("PASS")
    self._write_obs([
        {"ts": _iso(NOW), "beat": 1, "type": "diff", "session": "s", "payload": {
            "diff": "--- a/a.py\n+++ b/a.py\n+x=1\n",
            "untracked": ["new.py"],
            "touched_contents": {"a.py": "x=1"}}},
    ])
    state = load_state(self.cc / "nope.json")
    self._beat(state, TurnScheduler())
    row = json.loads(self.suggestions.read_text().splitlines()[-1])
    self.assertEqual(row["reviewed_files"], ["a.py", "new.py"])

def test_pass_verdict_saves_case_material(self):
    # same beat as above; PASS row id must have case-material JSON on disk
    ...
    material = self.cc / "case-material" / f"{row['id']}.json"
    self.assertTrue(material.exists())
    data = json.loads(material.read_text())
    self.assertIn("events", data); self.assertIn("latest_diff", data)
```

- [ ] **Step 2: Run tests, verify both FAIL** (`reviewed_files` KeyError; material missing).
- [ ] **Step 3: Implement** in `judge_batch`: build `reviewed_files` before the model call (it derives from `ctx["latest_diff"]` only); add to `record` dict next to `session`; move the existing `save_case_material` call out of the SUGGESTION-only branch so it runs for every non-ERROR verdict. Add `from observer.gitwatch import _touched_paths` (loop-boundary note: gitwatch is observer-side, but CLAUDE.md's shared-utilities exception covers small pure helpers — state this in a comment; alternatively copy the 6-line function if the reviewer objects).
- [ ] **Step 4: Run focused tests → PASS; full suite → PASS.**
- [ ] **Step 5: Commit** `"Critic: record reviewed files on every verdict, keep PASS packets"`.

---

### Task 2: Miss detection (pure logic)

**Files:**
- Create: `reflector/misses.py`
- Test: `tests/test_misses.py` (new file)

**Interfaces:**
- Consumes: suggestion rows (with Task 1's `reviewed_files`), commit-event dicts (`{"type":"commit","ts":...,"payload":{"subjects":[...],"diff":...}}`).
- Produces:
  - `FIX_RE: re.Pattern` (module constant)
  - `LOOKBACK_S = 3600` (module constant)
  - `detect_misses(pass_rows: list[dict], commit_events: list[dict], already_graded: set[str]) -> list[dict]` returning, per detected miss: `{"pass_id": str, "file": str, "commit_subject": str, "evidence": str}`. Task 3 consumes this.

**Design:** A PASS is a candidate miss when BOTH hold: (1) a commit event within `LOOKBACK_S` *after* the PASS's ts has a fix-shaped subject — `FIX_RE = re.compile(r"\b(fix|bug|revert|correct|repair|hotfix|regress)\w*\b", re.I)`; (2) that commit's diff modifies a file in the PASS's `reviewed_files` (substring match of the path against `+++ b/<path>` lines of the commit diff). Both conditions required — precision first (Global Constraints: a false `missed` grade poisons the eval harvest). Skip pass_ids in `already_graded`. One miss per PASS (first matching commit wins).

- [ ] **Step 1: Write failing tests** in `tests/test_misses.py`:

```python
def _pass_row(id="p1", ts=None, files=("a.py",)):
    return {"id": id, "verdict": "PASS", "ts": _iso(ts or NOW),
            "reviewed_files": list(files), "heuristics_version": 3}

def _commit(ts, subject, diff):
    return {"type": "commit", "ts": _iso(ts),
            "payload": {"subjects": [f"abc123 {subject}"], "diff": diff}}

# cases:
# fix-subject + file overlap within lookback   -> 1 miss, evidence names file+subject
# fix-subject, NO file overlap                 -> no miss
# file overlap, non-fix subject ("add docs")   -> no miss
# fix commit BEFORE the pass                   -> no miss
# fix commit past LOOKBACK_S                   -> no miss
# pass_id already in already_graded            -> no miss
# two matching commits                         -> exactly 1 miss (first)
# malformed ts on either side                  -> skipped, no crash
```

- [ ] **Step 2: Run → all FAIL** (module missing).
- [ ] **Step 3: Implement `reflector/misses.py`** (~60 lines; `_epoch` helper mirrors `reflector/judge.py`'s).
- [ ] **Step 4: Focused → PASS; full suite → PASS.**
- [ ] **Step 5: Commit** `"Reflector: detect missed catches — fix commits revising PASSed files"`.

---

### Task 3: Wire misses into grading, harvest, and metrics

**Files:**
- Modify: `reflector/main.py` (grade_pending: miss pass), `reflector/harvest.py` (accept `missed` outcome → must-flag case), `reflector/report.py` (missed column), `ui/server/council.ts` (mirror), `ui/src/types.ts`, `ui/src/components/ImprovementChart.tsx` (missed in the per-version breakdown bar)
- Test: `tests/test_reflector.py`, `tests/test_harvest.py`

**Interfaces:**
- Consumes: `misses.detect_misses(...)` (Task 2); `harvest.maybe_harvest(cc, suggestion_row, outcome)` (existing).
- Produces: outcome rows `{"suggestion_id": <pass id>, "outcome": "missed", "evidence": ..., "heuristics_version": ...}` in `outcomes.ndjsonl`; harvested `harvest-<id>.json` must-flag cases with `expect_files=[basename(miss file)]`; `build_rows` output gains `"missed": int` per version; dashboard breakdown shows it.

**Design:** In `grade_pending`, after the existing to_judge loop: collect PASS rows from the (already tail-read) suggestions, commit events from the (already tail-read) observations, call `detect_misses` with `graded_ids`, and for each miss append the outcome row (no model call — deterministic) and call `harvest.maybe_harvest` with outcome `"missed"` (guarded try/except like the existing harvest call). In `harvest.py`, `"missed"` maps to a must-flag case using `expect_files=[Path(miss_file).name]` — pass the file via a new optional `miss_file: str | None = None` parameter (PASS rows have no `suggestion` dict to read a file from). `missed` does NOT enter the acceptance-rate denominator (acceptance stays accepted/(accepted+rebutted+ignored)); it is its own column — the gate and rollback comparisons are unchanged (state this in a comment where `build_rows` computes acceptance; the two-signal separation rule in CLAUDE.md applies).

- [ ] **Step 1: Write failing tests:**

```python
# test_reflector.py
def test_missed_pass_gets_graded_and_harvested(self):
    # cc dir with: suggestions.ndjsonl (one PASS w/ reviewed_files + case-material file),
    # observations.ndjsonl (later fix commit touching that file), empty outcomes
    n = grade_pending(cc)
    outc = read_rows(cc / "outcomes.ndjsonl")
    self.assertEqual(outc[-1]["outcome"], "missed")
    self.assertTrue(any(p.name.startswith("harvest-") for p in HARVESTED.glob("*.json")))

def test_missed_grade_is_idempotent_across_passes(self):
    grade_pending(cc); before = len(read_rows(cc / "outcomes.ndjsonl"))
    grade_pending(cc); self.assertEqual(len(read_rows(cc / "outcomes.ndjsonl")), before)

def test_missed_not_in_acceptance_rate(self):
    rows = build_rows([...pass row...], [{"suggestion_id": "p1", "outcome": "missed",
                                          "heuristics_version": 3}])
    self.assertIsNone(rows[0]["acceptance"]); self.assertEqual(rows[0]["missed"], 1)

# test_harvest.py
def test_missed_outcome_creates_must_flag_case_with_miss_file(self): ...
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** (reflector/main.py, harvest.py, report.py). Then mirror `missed` in `council.ts` `buildCurve` + types + the `Breakdown` segments in `ImprovementChart.tsx` (one new segment, `bg-bad/40`, label "missed").
- [ ] **Step 4: Focused + full suite → PASS; `npx tsc --noEmit` + `npm run build` → clean.**
- [ ] **Step 5: Commit** `"Grade the silences: missed-catch outcomes, harvested as must-flag evals"`.

---

### Task 4: Investigation-based judgment (read-only repo tools)

**Files:**
- Modify: `critic/agent.py` (no code change expected — verify `ask(tools=, cwd=)` already supports this), `critic/main.py` (`ask_with_retry` passes tools+cwd), `critic/persona.md`, `critic/heuristics.seed.md`
- Test: `tests/test_agent.py`, `tests/test_critic.py`

**Interfaces:**
- Consumes: `agent.ask(prompt, system=..., tools=..., cwd=...)` (existing signature from the verify path).
- Produces: `critic/main.py` module constant `JUDGE_TOOLS = "read,grep,find,ls"` (read-only pi builtins — explicitly NOT `bash`, NOT `edit`/`write`); judgment turns run with `tools=JUDGE_TOOLS, cwd=str(ctx["repo"])` when `ctx.get("repo")` is set (eval replays pass no repo → tool-less, keeping frozen-case scoring hermetic).

**Design:** The persona gains an "Investigate before you speak" section: *"You have read-only tools on the developer's repo. Before flagging, check your suspicion — open the file, grep for the guard or test you believe is missing. A finding you could have refuted by looking is worse than silence. PASS verdicts do not require investigation."* The heuristics seed gains one rule: *"Before flagging 'X is missing', look for X with your tools; flag only if it is genuinely absent."* `ask_with_retry` signature stays `(text, ctx)`; it reads `ctx` for repo. Verification (`critic/verify.py`) is unchanged — investigation gets eyes, verification keeps hands.

- [ ] **Step 1: Write failing tests:**

```python
# test_agent.py — command construction (PI_BIN stub prints argv, CRITIC_CMD unset):
def test_ask_with_tools_and_cwd_builds_readonly_flags(self):
    # assert "--tools", "read,grep,find,ls" in argv; "bash" not in tools arg;
    # subprocess cwd == the temp dir (capture via a PI_BIN shim that echoes $PWD)

# test_critic.py — plumbing:
def test_judgment_turn_passes_repo_tools(self):
    # monkeypatch critic.main.agent.ask to capture kwargs; run a beat with ctx repo set
    self.assertEqual(captured["tools"], "read,grep,find,ls")
    self.assertEqual(captured["cwd"], str(self.repo))

def test_eval_scoring_stays_toolless(self):
    # evals.run.score_heuristics path: captured tools is None
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** (constant + two-line change in `ask_with_retry`; persona/seed edits verbatim from Design).
- [ ] **Step 4: Focused + full suite → PASS.**
- [ ] **Step 5: Commit** `"Critic investigates before speaking: read-only repo tools on judgment turns"`.

---

### Task 5: Rebuttals distill into repo knowledge

**Files:**
- Create: `reflector/knowledge.py`
- Modify: `reflector/main.py` (distill on rebutted grades), `reflector/persona.md` (TASK: DISTILL protocol), `critic/prompt.py` (render knowledge section), `critic/main.py` (load knowledge into ctx per judgment)
- Test: `tests/test_knowledge.py` (new), `tests/test_critic.py` (prompt rendering)

**Interfaces:**
- Consumes: rebutted grade rows (`grade["outcome"] == "rebutted"`, `grade["evidence"]`), `agent.ask` via `reflector/main._ask`.
- Produces:
  - `knowledge.py`: `KNOWLEDGE_MAX_FACTS = 30`; `build_distill_prompt(suggestion_row: dict, rebuttal_evidence: str) -> str` (first line `TASK: DISTILL`); `parse_fact(raw: str) -> str | None` (strips, rejects `NONE`/empty/`>240` chars/multi-line); `add_fact(cc: Path, fact: str) -> bool` (appends `- <fact>` to `.codecouncil/knowledge.md`, creates with header `# Repo knowledge (learned from past reviews)`, dedupes case-insensitively on normalized whitespace, evicts oldest beyond cap, atomic write via temp+rename); `load(cc: Path) -> str` (empty string when absent).
  - `critic/prompt.py`: `build_prompt(..., knowledge: str = "")` renders, when non-empty, a `REPO KNOWLEDGE (learned from past reviews — trust these over generic rules):` section immediately after HEURISTICS. `build_task_review` gains the same parameter.

**Design:** In `grade_pending`, when a grade lands as rebutted (both the explicit-marker path and the model-judged path), guardedly (`try/except Exception`) call `_ask(knowledge.build_distill_prompt(row, evidence))`, then `parse_fact` → `add_fact`; print one line when a fact is born. `reflector/persona.md` gains: *"## TASK: DISTILL — You get one code-review finding and the developer's rebuttal. Reply with exactly ONE sentence stating the repo-specific fact or convention that makes the finding wrong (max 200 chars), or the single word NONE if the rebuttal reveals no reusable fact. No preamble."* `critic/main.py` reads `knowledge.load(cc)` next to `ensure_heuristics` in `judge_batch`/`task_review` — fresh each call, same pattern as heuristics.

- [ ] **Step 1: Write failing tests:**

```python
# test_knowledge.py
def test_parse_fact_accepts_one_sentence_rejects_none_and_long(self): ...
def test_add_fact_dedupes_and_caps_at_30_evicting_oldest(self): ...
def test_distill_prompt_contains_marker_finding_and_rebuttal(self):
    self.assertTrue(build_distill_prompt(row, "guard exists").startswith("TASK: DISTILL"))

# test_reflector.py
def test_rebutted_grade_distills_a_fact(self):
    # CRITIC_CMD stub: replies rebut-JSON for TASK: GRADE prompts,
    # "Tests are stdlib unittest run via discover." for TASK: DISTILL prompts
    grade_pending(cc)
    self.assertIn("stdlib unittest", (cc / "knowledge.md").read_text())
def test_distill_failure_never_kills_grading(self): ...  # _ask raises on DISTILL only

# test_critic.py
def test_prompt_renders_knowledge_after_heuristics(self): ...
def test_no_knowledge_no_section(self): ...
```

- [ ] **Step 2: Run → FAIL.**  **Step 3: Implement.**  **Step 4: Focused + full suite → PASS.**
- [ ] **Step 5: Commit** `"Rebuttals become repo knowledge the critic reads on every judgment"`.

---

### Task 6: Rule attribution — grades attach to the heuristics that caused them

**Files:**
- Modify: `critic/persona.md` (cite the rule), `critic/prompt.py` (number the rules when rendering; keep `rule` in parsed suggestion), `reflector/main.py` (copy `rule` onto outcome rows), `reflector/report.py` (per-rule table: `build_rule_rows`), `reflector/rewrite.py` (per-rule stats in the rewrite prompt)
- Test: `tests/test_critic.py`, `tests/test_reflector.py`

**Interfaces:**
- Consumes: heuristics text (top-level `- ` bullets — same convention `ui/server/council.ts heuristicsRules` already parses).
- Produces:
  - `prompt.numbered_heuristics(text: str) -> str`: renders each top-level bullet as `R1.`, `R2.`, … (continuation lines untouched); used inside `build_prompt`/`build_task_review` for the HEURISTICS section.
  - Suggestion JSON schema gains optional `"rule": <int|null>`; `parse_reply` keeps it when it's a positive int, else null.
  - Outcome rows gain `"rule": <int|null>` (copied from the suggestion row being graded).
  - `report.build_rule_rows(suggestions, outcomes) -> list[dict]`: per (heuristics_version, rule): suggested/accepted/rebutted/ignored counts. `python3 -m reflector.report` prints it as a second table.
  - `rewrite.build_prompt` includes, per rule of the current version, one line `R3: 2 suggested, 0 accepted, 2 rebutted` and the instruction *"Prefer dropping or sharpening rules with rebuttals/ignores; preserve rules with accepts."*

**Design:** Persona output protocol changes ONE line — the JSON gains `"rule": <number of the heuristic that most motivated this finding, or null>`. `parse_reply` must not reject legacy replies without `rule` (default null). Numbering is positional per version — stable within a version, which is the only scope grades compare within (state this in a `numbered_heuristics` docstring; cross-version rule identity is deliberately out of scope, YAGNI).

- [ ] **Step 1: Write failing tests:**

```python
# test_critic.py
def test_numbered_heuristics_renders_stable_indices(self):
    out = prompt.numbered_heuristics("version: 3\n- first rule\n  cont\n- second")
    self.assertIn("R1. first rule", out); self.assertIn("R2. second", out)
def test_parse_reply_keeps_valid_rule_defaults_null(self):
    self.assertEqual(prompt.parse_reply('{"file":"a.py","issue":"x","rule":3}')["suggestion"]["rule"], 3)
    self.assertIsNone(prompt.parse_reply('{"file":"a.py","issue":"x"}')["suggestion"]["rule"])
    self.assertIsNone(prompt.parse_reply('{"file":"a.py","issue":"x","rule":"nope"}')["suggestion"]["rule"])

# test_reflector.py
def test_outcome_rows_carry_rule(self): ...          # grade_pending copies it
def test_build_rule_rows_aggregates_per_version_rule(self): ...
def test_rewrite_prompt_includes_per_rule_stats(self):
    self.assertIn("R3: ", rewrite.build_prompt(current, 3, outcomes_with_rules))
```

- [ ] **Step 2: Run → FAIL.**  **Step 3: Implement.**  **Step 4: Focused + full suite → PASS.**
- [ ] **Step 5: Commit** `"Rule attribution: every grade traces to the heuristic that caused it"`.

---

## Deferred (separate future plans — do not build here)

- **Council mode:** 2–3 decorrelated-model councilors with an agreement filter. Depends on judgment-turn cost data after Task 4.
- **PR mode + agent trust ledger:** CI entry point reviewing PR diffs; receipts aggregated into longitudinal claim-accuracy metrics.

## Self-review notes

- Spec coverage: roadmap items 1→Task 1-3, 2→Task 4, 3→Task 5, 4→Task 6; items 5-6 explicitly deferred. ✓
- Type consistency: `reviewed_files: list[str]` (T1) is what `detect_misses` reads (T2) and `grade_pending` passes (T3); `maybe_harvest(..., miss_file=...)` new kwarg only in T3; `build_prompt(knowledge=...)` (T5) and `numbered_heuristics` (T6) both touch the HEURISTICS section — T6 must preserve T5's section ordering (heuristics, then knowledge). ✓
- Known tension to watch in review: Task 1 imports `observer.gitwatch._touched_paths` across a loop boundary — flagged in-task with the copy fallback.
