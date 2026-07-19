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

## Numbers to memorize

- **~19 seconds** from an edit to a logged verdict (was ~3 minutes before the
  latency work). The lying-commit catch took **88s** including sandbox
  verification.
- Beats: observer every **3s**, critic every **10s** — but model calls only
  fire when code actually changed, with a **45s** floor between calls, so
  cost stays flat no matter how fast the beats are.
- Verdict record: **~25+ verdicts** on our own repo, **1 false positive**
  (rebutted on the record), the rest correct PASSes or catches.
- Training: **10 unscripted sessions** → 3 catches (2 sandbox-verified) →
  2 delivered → **1 accepted, 1 rebutted** — 50% acceptance, 100% agreement
  between model grades and the code-based cross-check.
- **Three self-rewrites** (v1→v2→v3), each triggered by real graded outcomes,
  each recorded in the agent's own ledger with the grades that caused it.
- Frozen evals (7 cases, two repetitions where available): **v1 ≈ 71%,
  v2 ≈ 79%, v3 ≈ 71%**. v2 — trained on an acceptance — got sharper and
  caught more; v3 — trained on three rebuttals — got more cautious and gave
  one catch back. The behavior tracks its feedback in both directions, and
  the eval makes both directions visible. That's the point: measured
  self-modification with regression detection, not a marketing curve.
- In-the-wild acceptance per version: v1 50% (n=2), v2 0% (n=3 — two
  rebutted, one ignored; the rebuttals are what taught v3 caution).
- Model: Nemotron 3 Super (120B). We benchmarked the 550B Ultra: **no
  difference** in accuracy or speed on our cases.

## FAQ — common judge questions

**Q: How is this "recursive self-improvement" and not just a code reviewer?**
The reviewer's judgment lives in one rules file it does not share with us: the
Reflector grades real outcomes and rewrites that file, version by version. The
peer-review product is the substrate; the thing being improved is the agent's
own instructions, by the agent, from measured results.

**Q: What's the heartbeat here?** Three, actually: the Observer ticks every
3s, the Critic every 10s, the Reflector every few minutes. The whole system is
autonomous once started — no human prompts it.

**Q: Why not just a linter / CI / tests?** Those check code against rules or
code against tests. CodeCouncil checks code against **what the author claimed**
— "I handled X", a docstring's promise, a commit message's "tested and ready."
That mismatch class is invisible to every linter and most test suites, and
it's the signature failure of AI coding agents.

**Q: How do suggestions actually reach the coding agent?** Through Claude
Code's official hooks: injected as context right after file edits, and for
high-severity findings, a one-time block when the agent tries to declare the
task finished. The agent must fix it or say why it disagrees — both outcomes
are captured and graded.

**Q: What stops two AIs from arguing forever?** Hard caps: each finding is
delivered at most once per channel, blocks completion at most once, and a
reasoned "I disagree" counts as resolved (and is valuable training data).
Findings expire after 10 minutes so stale critiques never resurface.

**Q: What if the critic is wrong?** Three defenses, all demonstrated: it must
verify findings by running code in its sandbox first (refuted findings are
never delivered); the coding agent can rebut (recorded honestly, not hidden);
and rebuttals feed the next heuristics rewrite so the same mistake fades.

**Q: Why so quiet? / Does it interrupt constantly?** The opposite — PASS is
the designed default, and every PASS states its reason (visible on the
dashboard). A peer that speaks rarely gets listened to; that discipline is
written into its rules file and survives every rewrite.

**Q: What's the cost profile?** Model calls only happen when code changed —
an idle hour costs zero calls. Active coding costs roughly one short call per
45+ seconds, plus one verification call per (rare) finding.

**Q: Is my code safe?** Everything runs locally: transcripts and diffs are
read from your machine, and the only thing leaving is the review prompt to
routed inference. The critic itself runs in a network-restricted sandbox and
never holds an API key. Its code-execution (verification) happens inside that
sandbox, never on your machine.

**Q: Does it work with Cursor / other editors?** The architecture needs two
things: an intent stream and a feedback channel. Claude Code exposes both
(transcripts + hooks), so we built there first. Cursor is closed on both
counts today; anything that logs agent reasoning can be adapted.

**Q: Couldn't the coding agent game the reviewer?** The mechanical checks
can't be argued with: "tests pass" is checked against whether a test command
actually executed; "accepted" grades are cross-checked against whether the
flagged file really changed. The dashboard exposes the exact prompt behind
every verdict, so nothing is staged.

**Q: What would you build next?** Multi-reviewer council (the name is waiting
for it), richer eval sets that grow automatically from graded outcomes, and
adapters beyond Claude Code.

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

## Suggested demo order

1. **Split screen**: Claude Code coding on one side, dashboard on the other —
   point out the live thinking stream and the beat pulse.
2. **The lying commit** (the money shot): commit code whose message claims
   something the code doesn't do → catch lands in ~90s with the sandbox
   verification chip → click "show what the critic saw."
3. **The steering**: show the transcript where a coding agent, asked only to
   add a docstring, fixed a flagged bug because the finding was injected —
   then got blocked at "done" and rebutted cleanly.
4. **The self-improvement**: heuristics card — the version badge, the +/−
   rules diff, the dated rewrite ledger — then the two charts: acceptance per
   version and the frozen-eval scores.
5. **Close with honesty**: show the rebutted finding recorded as rebutted.
   Judges trust systems that admit their misses.

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
