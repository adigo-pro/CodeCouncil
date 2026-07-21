# Hardening & Genuine Usefulness Plan

Sources: fresh-eyes subagent review (scratchpad/fresh-eyes-review.md, 10 findings, all
file:line-grounded), product exploration, and live dogfood evidence from this session
(critic misattributed two findings to a reviewer session; reviewer rebutted via
COUNCIL-REBUTTAL — confirming Tasks 2 and 8 below are real, not theoretical).

Global constraints: Python stdlib-only; all loops communicate only via .codecouncil/ files;
peer_hook.py stays fail-open; NDJSON readers tolerate partial trailing lines; tests are
stdlib unittest with CRITIC_CMD stubbing; every task lands with tests and a passing suite.

## Tier 1 — Trust (a developer turns it off the first time any of these bites)

### Task 1: Secret redaction before anything leaves the machine
Findings context (fresh-eyes #1): observer/gitwatch.py ships diffs (50K chars) and untracked
file contents (4K/file) verbatim to the model backend; the prompt even asks the model to hunt
for secrets — catching one requires leaking it first.
Do: add `core/redact.py` — regex pass for high-confidence credential shapes (AWS `AKIA…`,
`nvapi-…`, `sk-…`, `ghp_…`, PEM headers, `(KEY|TOKEN|SECRET|PASSWORD)\s*[=:]\s*<high-entropy>`)
replacing values with `«REDACTED:kind»`. Apply in observer/gitwatch.py at capture time (diff,
untracked_contents, commit diffs) so nothing downstream (prompts, dashboard) ever holds the value.
Innovation: the marker itself is the signal — teach critic/persona.md + heuristics seed that
`«REDACTED:…»` in a diff IS a confirmed secret-in-code finding; flag it without ever seeing it.
Tests: redaction shapes; marker survives into prompt; fingerprint stability (no diff-event spam).

### Task 2: Session-scoped suggestion delivery
Dogfood-proven bug: hooks deliver every repo suggestion to every session (reviewer session got
implementer findings). Do: critic tags each suggestion with the source `session` (majority
session of the judged batch events, already on each event); hooks/logic.py delivers a suggestion
only when the hook event's `session_id` matches (or suggestion has no session tag — task reviews
stay repo-wide). hooks/peer_hook.py already receives session_id in the event JSON.
Tests: matching/mismatched/absent session tags on both channels.

### Task 3: TTL stamped at write time, not dispatch time (fresh-eyes #5)
Verified findings burn 300–360s of their 600s TTL in model round-trips before hitting disk.
Do: in critic/main.py judge_batch/task_review, restamp `record["ts"] = now_iso()` immediately
before append (keep original as `dispatched_ts` for latency metrics). hooks/logic._age_ok then
measures real delivery window. Tests: record ts ≥ dispatch ts; age check uses write ts.

### Task 4: Ledger locking + bounded hook reads (fresh-eyes #3, #8)
Do: (a) hooks/peer_hook.py wraps the load→decide→save of delivered.json in an `fcntl.flock`
on a sidecar lockfile (fail-open on any lock error); (b) hook + verdict_history + reflector
read suggestions/outcomes via `read_tail_rows` (they only need TTL-window rows / last 5).
Tests: two simulated concurrent deliveries don't double-mark or lose marks; tail-read paths.

### Task 5: Malformed-reply visibility (fresh-eyes #7)
Silent PASS degradation can mute the critic indefinitely (provider/format drift).
Do: render_verdict prints a warning on `malformed`; critic tracks rolling malformed count in
critic-state.json; ui/server/council.ts surfaces `malformedRecent` badge in stats; dashboard
Header shows it. Tests: parse → state counter; server aggregation.

### Task 6: Don't lose in-flight batches on crash (fresh-eyes #4)
Offset persists before the judgment thread lands; a crash silently drops the batch.
Do: critic/main.py keeps `committed_offset` in state — advanced only inside judge_batch after
the record append; on startup, resume from committed_offset. The scheduler already serializes
turns, so at most one batch is uncommitted. Tests: simulated crash-before-append replays batch;
no double-judgment on clean path.

## Tier 2 — Self-improvement becomes measured, reversible control (the headline claim, made honest)

### Task 7: Eval-gated heuristics rewrites with auto-rollback (fresh-eyes #2)
Do: reflector runs evals/run's scoring (refactor its per-version scoring into an importable
function) on the CANDIDATE heuristics before apply(); reject if score drops vs current version.
After apply, if the next N graded outcomes show acceptance strictly below the archived version's,
auto-revert from heuristics-history/ (one revert max per version, recorded in reflections.ndjsonl).
Tests: candidate scoring gate (stubbed model), revert path, revert-once guard.

### Task 8: Auto-harvest eval cases from graded outcomes
The eval set (7 hand-frozen cases) becomes self-growing: an `accepted` outcome freezes its
prompt inputs as a must-flag case; a rebutted-with-reason (or verification-refuted) finding
freezes as a must-pass case. Cap harvested set (e.g. 40), dedupe by flagged file+issue hash.
This closes the real loop: outcomes → eval cases → gate future rewrites (Task 7).
Tests: harvest from synthetic outcomes; dedupe; cap.

### Task 9: Sticky tests-run fact (fresh-eyes #6; dogfood-confirmed false flag at beat 355)
Do: observer or critic tracks `tests_run_at` timestamps per session in state (matching
prompt.tests_run patterns as events stream); task review's MECHANICAL FACT uses whole-session
history, not the rolling window. Tests: test command 2h before "done" is still credited.

## Tier 3 — Genuinely useful (move the value to where a developer actually decides)

### Task 10: Session receipt at Stop
The artifact a human reads. On task review (agent claims done), critic also writes
`.codecouncil/receipts/<session>-<ts>.md`: claims made (from reasoning/commits), what was
mechanically verified (tests run? files claimed == files changed?), findings raised with
outcomes (fixed/rebutted/pending). Hook's Stop response appends one line telling the agent the
receipt path so it lands in the transcript for the human. Dashboard lists receipts.
Tests: receipt content from synthetic window; hook line present exactly once.

### Task 11: Critic sees touched files, not just hunks
File-blind judging is the top false-positive source. Do: observer includes capped current
contents of files touched by the diff (like untracked_contents; e.g. 6K/file, 24K total,
redacted per Task 1) in diff events as `touched_contents`; prompt.py renders them after the
diff under the existing budget trimming (diff still trims last). Tests: capture, caps, budget.

### Task 12: CI
GitHub Actions: python unittest matrix (3.10/3.12) + `tsc --noEmit` + vite build. The
self-reviewing tool should at minimum machine-verify itself on push.

Deferred (explicitly not now): multi-suggestion batches (fresh-eyes #10 — one-issue discipline
is the product's voice); observations rotation (offsets make it risky; bounded reads already
cap cost); evidence-window sizing by pending-suggestion age (fresh-eyes #9) — reconsider after
Task 8 provides better grading data.
