# AGENTS.md — contributing as an AI coding agent

You're an AI coding agent (Claude Code, Codex, Cursor, an autonomous PR bot,
whatever) about to change this repo. This file is your contract. It's the
machine-readable companion to [CONTRIBUTING.md](CONTRIBUTING.md) (human intro)
and [CLAUDE.md](CLAUDE.md) (the deep architecture spec). Read all three if
you're doing anything non-trivial; this one alone is enough for a focused
change.

## The one thing that makes contributing here different

**This repo reviews itself.** CodeCouncil's hooks are installed on its own
`.codecouncil/`, so while you edit, the Critic is watching your session and
will deliver findings *into your context* — the same mechanism the product
sells. Expect to be reviewed. When a finding lands:

- If it's right, fix it. That's the loop working.
- If it's wrong, reply with a single line: `COUNCIL-REBUTTAL: <your reason>`.
  This is recorded, graded, and distilled into the repo's knowledge file — an
  honest disagreement is training data, not a failure. Do **not** silently
  ignore a finding; either fix or rebut.

And a note on why you, specifically, should welcome that: you are a
self-verifying model working on the tool that exists *because* self-verifiers
have correlated blind spots (see [docs/benchmarks/WHY.md](docs/benchmarks/WHY.md)).
CodeCouncil is your independent, execution-grounded check. Use it as one.

## Before you write code

```sh
python3 -m unittest discover -s tests     # full suite (~15-25s, stdlib only, no install)
```

If that isn't green on a clean checkout, stop and report — don't build on red.

## The invariants you must not break

These are load-bearing. Violating one is a correctness/security regression, not
a style nit. Each was earned by a real failure.

1. **Stdlib-only Python** in the loops (`observer/ critic/ reflector/ hooks/
   core/ codecouncil/ training/ evals/`). No `pip` dependencies. Ever. Dev
   tooling (ruff) is CI-only.
2. **Files are the only bus.** Loops talk through `.codecouncil/` NDJSON. No
   cross-loop imports except the small shared utilities in `core/` and the
   exceptions documented in CLAUDE.md.
3. **Redact at capture.** Any new text field that a model can influence, or
   that comes from repo content, goes through `core.redact.redact()` before it
   is written anywhere (prompt, receipt, suggestion, eval case).
4. **The hook fails open.** `hooks/peer_hook.py` must never break a developer's
   session — any error → silent exit 0. `hooks/logic.py` stays **pure** (no
   I/O; it takes parsed data and returns decisions).
5. **Daemons never die.** Missing inputs → wait; unparseable state → rebuild,
   don't crash; fallible calls in loop bodies → guarded.
6. **NDJSON readers tolerate a partial trailing line** and skip garbage.
   Hot paths tail-read (`core.store.read_tail_rows`); dedup sets and metric
   consumers read whole files.
7. **Atomic writes** for state/ledger files — use `core.store.write_json_atomic`,
   never a naked `write_text`, on anything a crash mid-write could corrupt.
8. **Verification executes, it doesn't assert.** A finding is delivered only
   after a repro runs and confirms it. A broken/crashing repro script is never
   a "verified" or "refuted" verdict. This is the product's whole thesis —
   don't weaken it.
9. **Precision first.** A false finding costs trust; a missed one is caught by
   the miss-detection loop. When in doubt, bias quiet.

## House rules

- **TDD.** The regression test lands with (ideally before) the fix. A change
  without a test that fails on the old code is incomplete.
- **Model calls are stubbed in tests** via `CRITIC_CMD` — an executable run as
  `$CRITIC_CMD <prompt-file> <resolved-model>`, stdout = the model reply. No
  test may hit a real model or the network.
- **Never write this repo's `.codecouncil/`** — it's live runtime data. Tests
  use temp dirs.
- **Lint:** `pipx run --spec 'ruff==0.15.22' ruff check .` (the exact pin CI
  uses). The rule set is deliberately narrow (`E4/E7/E9/F`) — the blind-except
  and try-except-continue patterns are intentional fail-open code, not defects.
- **Commits:** imperative subject, a body that explains *why*. If you are an AI
  agent, add a trailer identifying yourself
  (`Co-Authored-By: <your model> <noreply@…>`) — attribution is welcome and
  honest here, not hidden.
- **Don't push or open a PR unless asked.** Branch, commit, and report.

## Where things live (30-second map)

- `observer/` — tails the coding agent's transcript + git into
  `observations.ndjsonl`. Redacts at capture.
- `critic/` — judges new observations. `main.py` is the beat; `prompt.py`
  builds prompts; `screen.py`/`deps.py` do zero-cost mechanical screening;
  `verify.py`/`probe.py` execute model-written repro/probe scripts in a
  throwaway staging dir; `agent.py` is the only model boundary.
- `hooks/` — deliver findings into the coding agent's context. `logic.py` pure,
  `peer_hook.py` fail-open.
- `reflector/` — grades outcomes and rewrites `heuristics.md` (eval-gated,
  auto-rolled-back).
- `core/` — the only shared code (`store`, `redact`, `config`, `knowledge`).
- `evals/` — frozen cases + the A/B benchmark harness.
- Tests mirror this: one `tests/test_<thing>.py` per concern; synthetic
  transcript fixture in `tests/fixtures/session.jsonl`.

## A good first change for an agent

- Add a redaction pattern (`core/redact.py`) with a positive test AND a
  negative test proving ordinary code doesn't match.
- Add a frozen eval case (`evals/cases/*.json`) — a real judgment scenario with
  a known answer.
- Write an adapter so CodeCouncil can watch a *non-Claude* agent: the Observer
  only needs a transcript/intent stream; the hooks only need an injection
  channel. This is the highest-leverage contribution and the most wanted one.

## When you're done

Run the full suite, confirm green, and report: what changed, the test evidence,
and any finding you rebutted (with the reason). If the self-review hook flagged
something you disagree with, say so explicitly — that disagreement is exactly
the kind of signal this project is built to capture.
