# CodeCouncil — Project Guide

*The simple version, for explaining to anyone (including judges).*

## One sentence

CodeCouncil is an AI peer reviewer that watches your AI coding agent work in
real time, interrupts only when something is genuinely wrong, **proves its
findings by running code before speaking** — and rewrites its own review
instructions based on which of its suggestions turned out to be right.

## The problem

AI coding agents (Claude Code, Cursor, etc.) are fast but overconfident. The
most common failure isn't broken syntax — it's the agent **saying** something
that isn't true: "handled the edge case" (it didn't), "all tests pass" (it
never ran them), a comment promising behavior the code doesn't have. Nobody is
reading the diff to check. CodeCouncil is the second pair of eyes that does.

## How it works — four loops

Think of it as four simple programs passing notes through files:

1. **Observer** (every 3 seconds): reads the coding agent's session transcript
   (what it *said* it's doing) and the git diff + commits (what *actually*
   changed). Writes both streams to a log. Pairing intent with reality is the
   core trick — that pairing is what no linter has.

2. **Critic** (every 10 seconds): when code actually changed, sends one
   question to an LLM running in a locked-down sandbox: *"Is there ONE issue
   here worth interrupting a developer for?"* The answer is almost always
   PASS (with a stated reason). When it does flag something, it first
   **verifies the finding by writing and running a repro in its sandbox** —
   findings it refutes are never delivered. It also runs a special "task
   review" when the agent declares work finished: are the completion claims
   actually supported by the code? (Including a mechanical check: did a test
   command ever actually run?)

3. **Hooks** (instant): findings are injected directly into the coding
   agent's own context — after edits for medium ones; high-severity ones can
   block the agent from declaring "done" until fixed or rebutted. This is how
   the reviewer *steers* rather than just talks.

4. **Reflector** (every few minutes): looks back at each delivered suggestion
   and grades what happened next — **accepted** (the code changed as
   suggested), **rebutted** (the agent disagreed with reasons), or
   **ignored** — then rewrites the Critic's rules file and bumps its version.
   The Critic reads that file on every judgment, so its behavior actually
   changes. That's the recursive self-improvement.

Everything is observable on a live dashboard: the agent's thinking stream,
every verdict with the exact prompt behind it, delivery + outcome per
suggestion, the rules file's version history with diffs, and the improvement
charts.

## How we measure "getting better" (two independent ways)

1. **Acceptance curve (in the wild):** every suggestion is tagged with the
   rules-version that produced it; we chart what fraction were accepted, per
   version. Cross-checked by a model-free signal (did the flagged file
   actually change afterward?).
2. **Frozen eval set (controlled):** 7 real cases with known right answers
   (4 bugs it must catch, 3 clean changes it must pass). Every rules version
   is replayed against the *same* cases — same inputs, only the learned rules
   differ. This catches regressions too: our first rewrite got better at the
   two categories it was trained on and *worse* at one other — visible in the
   table, which is exactly the point of having it.

## The proof it works (all real, none staged)

- **The lying commit:** we committed code claiming "exponential backoff and
  guaranteed error propagation" that did neither. Caught in 88 seconds,
  verified in the sandbox, delivered.
- **The full unscripted loop:** during training, the Critic spotted a
  docstring that promised `None` on bad input while the code raised an
  exception. The finding was injected into a *different* coding session,
  which fixed the bug and added tests unprompted. The Reflector graded it
  `accepted` from the commit evidence. Nobody scripted any step.
- **Honest disagreement:** another finding was rebutted by the coding agent
  ("deliberately left this") — recorded as `rebutted`, not spun as a win.
- **It reviews its own builders:** the hooks are installed on the CodeCouncil
  repo itself; it flagged its own author's work mid-session (and the author
  rebutted, on the record).

## Tech stack (and why judges should care)

- **Claude Code transcripts + hooks** — structured intent data and a
  sanctioned way to inject feedback into a running agent. No screen-scraping.
- **NemoClaw + OpenClaw sandbox, NVIDIA Nemotron 3 Super** — all model calls
  go through routed inference; the reviewer never holds an API key, and its
  code-executing verification runs inside the sandbox, not on your machine.
  We benchmarked the 4.5×-larger Nemotron Ultra: same accuracy, same speed —
  every quality gain came from better inputs, not a bigger model.
- **Zero-dependency Python** for the three loops; React dashboard.

## Honest answers to hard questions

- *"Isn't the model just guessing?"* — Findings ship with sandbox-executed
  proof, every verdict's exact input prompt is stored and viewable, and a
  model-free cross-check audits the grades.
- *"Did it catch anything you didn't plant?"* — The training defects were
  latent in the repo, not scripted into any instruction; the agent sessions
  that hit them, fixed them, or rebutted them didn't know they were tests.
  On its own authors it has mostly (correctly) stayed quiet — our workflow
  runs tests constantly, which is the one audience that needs a peer least.
- *"Is it improving itself, or are you improving it?"* — Both, and we keep
  them separate: our engineering improvements are git commits; *its*
  improvements are versioned rules rewrites in its own ledger, each traceable
  to the graded outcomes that caused it, each measurable on the frozen evals.
- *"What's the weakest part?"* — Small sample sizes on the curves (hours old,
  not weeks), and one-model-judging-another for grades — mitigated by the
  deterministic cross-checks, but honestly noted.

## Running it

```sh
python3 -m observer /path/to/repo        # eyes
python3 -m critic /path/to/repo          # judgment (+ sandbox verification)
python3 -m reflector /path/to/repo       # grading + self-rewrites
python3 -m hooks.install /path/to/repo   # steering (once per repo)
cd ui && npm run dev                     # dashboard at localhost:4700
python3 -m training.run                  # scripted session to generate data
python3 -m evals.run /path/to/repo       # score every rules version
```
