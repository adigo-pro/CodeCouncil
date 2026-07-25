# Contributing

CodeCouncil is small on purpose: four Python loops (stdlib-only), one React
dashboard, files as the only bus. Most contributions need nothing but Python
3.10+ and git.

## Setup

```sh
git clone https://github.com/adigo-tamu/CodeCouncil && cd CodeCouncil
python3 -m unittest discover -s tests        # no install step — stdlib only
cd ui && npm install && npm run dev          # dashboard (optional)
```

To run the system itself you need [pi](https://pi.dev)
(`npm install -g @earendil-works/pi-coding-agent`) and a model key in
`~/.codecouncil/env` — see the README.

## The invariants (please don't break these)

They exist because each one was earned by a real failure — see
`docs/PROJECT_GUIDE.md` and the commit history:

1. **Stdlib-only Python.** No pip dependencies in the loops. Ever.
2. **Files are the only bus.** Loops communicate through `.codecouncil/`
   NDJSON; no cross-loop imports beyond the small shared utilities in
   `core/` and the documented exceptions in `CLAUDE.md`.
3. **Redact at capture.** Any new text field entering events goes through
   `core.redact.redact()` before it is written anywhere.
4. **The hook fails open.** `hooks/peer_hook.py` must never be able to break
   a developer's session; `hooks/logic.py` stays pure (no I/O).
5. **Daemons never die.** Missing inputs → wait; unparseable state → rebuild;
   fallible calls in loop bodies → guarded.
6. **NDJSON readers tolerate partial trailing lines** and skip garbage.
   Unbounded-growth files are tail-read on hot paths; dedup sets and metric
   consumers read whole files.
7. **Model calls stub via `CRITIC_CMD`** in tests (`$CRITIC_CMD <prompt-file>
   <resolved-model>`, stdout = reply). No test may hit a real model.
8. **Precision first.** A false finding costs trust; a missed one is caught
   by the miss-detection loop. When in doubt, bias quiet.

## Workflow

- Tests are stdlib `unittest`, one file per concern in `tests/` (the
  critic has several: parsing/prompts, beat/scheduler, receipts, council).
  TDD is the house style: the regression test lands with (ideally before)
  the fix.
- Run the full suite before pushing; CI runs five jobs: the Python suite on
  3.10/3.12, the UI typecheck + build, a zero-API-spend bench-selftest (the
  A/B harness's safety scorers prove they discriminate good from bad before
  anyone trusts a live run), `ruff check .`, and an installer smoke test.
- Commit messages: imperative subject, a body that explains *why*.
- The repo watches itself during development — the critic may review your
  work as you code. If it flags something wrongly, reply with a line
  `COUNCIL-REBUTTAL: <your reason>`; honest disagreement is training data.

## Good first contributions

- New redaction patterns (with negative tests — ordinary code must not match).
- Eval cases (`evals/cases/`): real judgment scenarios with known answers.
- An adapter for another coding agent (the observer only needs an intent
  stream; the hooks only need an injection channel).
- Bake-off data for more models (`docs/benchmarks/` shows the format).

## Development loop

No venv, no pip install — the loops are stdlib-only Python 3.10+.

```sh
python3 -m unittest discover -s tests     # everything (~10s)
python3 -m unittest tests.test_critic     # one slice
pipx run ruff check .                     # same lint CI runs
```

Model calls are stubbed in tests via `CRITIC_CMD` — an executable invoked
as `$CRITIC_CMD <prompt-file> <resolved-model>` whose stdout becomes the
model's reply. To poke a loop by hand against a scratch repo:

```sh
printf '#!/bin/sh\necho "PASS — stub"\n' > /tmp/stub && chmod +x /tmp/stub
git init /tmp/scratch && git -C /tmp/scratch commit --allow-empty -m init

# observer requires a Claude Code session transcript for the repo it's
# watching; fake a minimal one so the scratch repo doesn't need a real
# Claude Code session pointed at it.
mkdir -p /tmp/scratch-home/.claude/projects/x
printf '{"type":"user","cwd":"%s"}\n' "$(cd /tmp/scratch && pwd -P)" \
    > /tmp/scratch-home/.claude/projects/x/s.jsonl

HOME=/tmp/scratch-home python3 -m observer /tmp/scratch --once
CRITIC_CMD=/tmp/stub HOME=/tmp/scratch-home python3 -m critic /tmp/scratch --once
```

Where things live: `observer/` tails transcripts + git into
`.codecouncil/observations.ndjsonl`; `critic/` judges new observations
(`critic/main.py` is the beat, `critic/prompt.py` builds prompts,
`critic/verify.py` runs repros in a staging dir); `hooks/` delivers
(`logic.py` is pure — test it without I/O); `reflector/` grades and
rewrites `heuristics.md`; `core/` is the only shared code. Tests mirror
this: one `tests/test_<thing>.py` per concern, real session transcript in
`tests/fixtures/session.jsonl`.
