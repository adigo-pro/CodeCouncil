# CodeCouncil

An AI peer programmer that watches your Claude Code session and, over time, gets
measurably better at critiquing your work. Four loops:

1. **Observer** — event-driven daemon pairing the agent's *intent* (transcript
   reasoning + tool calls) with what actually *changed* (git diffs + commits).
   Everything is **redacted at capture**: credentials in diffs, new files,
   commands, reasoning, or commit messages become `«REDACTED:kind»` markers
   before any text leaves your machine — and the marker itself is a finding.
2. **Critic** — reads observations, mostly says PASS, occasionally flags one
   verified high-value issue (findings are repro'd before delivery; refuted ones
   never ship). Sees the full current contents of changed files, not just hunks.
   When the agent declares work done, it runs a task review and writes a
   **session receipt** — claims vs mechanically-verified facts — to
   `.codecouncil/receipts/`. Judgment runs as a headless [pi](https://pi.dev)
   agent turn (`pi -p`) — any provider pi supports, including OpenAI-compatible
   endpoints and local models.
3. **Hook injection** — Claude Code hooks deliver findings into the coding
   agent's own context, scoped to the session whose work produced them:
   medium/high injected after edits (PostToolUse), high blocks completion once
   (Stop) until fixed or rebutted (`COUNCIL-REBUTTAL: <reason>` records an
   honest disagreement). New receipts are announced into the transcript once.
4. **Reflector** — grades delivered suggestions against what actually happened
   next (accepted / rebutted / ignored), then rewrites the Critic's heuristics
   from the grades. Rewrites are **eval-gated** (a candidate must match or beat
   the current rules on frozen cases), **auto-rolled-back** on measured
   regression, and the eval set **grows itself** from graded outcomes. The
   recursive self-improvement loop — measured, reversible, honest.

## Quick start — one command

```sh
python3 -m codecouncil /path/to/repo-being-coded-in   # hooks + all three loops
```

Requires [pi](https://pi.dev) with a provider configured (see below). Pass
`--model provider/id` to pick the model, `--no-hooks` to skip hook install.
The sections below run each loop individually.

## Run the Observer

```sh
python3 -m observer /path/to/repo-being-coded-in   # event-driven, 10s floor
python3 -m observer . --once                       # single beat
python3 -m observer . --from-start                 # replay transcripts from the top
```

Beats fire the moment a session transcript grows; `--interval` is only the
fallback ceiling that catches git-only changes.

Output: live terminal narration + `.codecouncil/observations.ndjsonl` in the watched
repo (one JSON event per line: `reasoning`, `tool_call`, `diff` — diffs include
capped contents of new untracked files so the Critic can see brand-new code).
No dependencies — Python 3.10+ stdlib only.

## Run the Critic

```sh
python3 -m critic /path/to/repo-being-coded-in    # 10s beat, model call only when code changed
python3 -m critic . --once                        # single beat
```

Each prompt carries a project-identity header, the critic's recent verdicts with
their outcomes (rebutted findings are settled), windowed events, and new-file
contents. Model calls happen only when code actually changed and run on a worker
thread, so the heartbeat never blocks (`--judge-every-beat` to judge everything).

Requires [pi](https://pi.dev) (`npm install -g @earendil-works/pi-coding-agent`)
with a provider configured — run `pi` once and `/login`, or set an API key env
var. `COUNCIL_MODEL=provider/model` overrides pi's default model; the persona
is `critic/persona.md`, passed via `--system-prompt`. Verification runs the
repro in a throwaway staging directory with pi's read/bash tools.

**Zero pi-login option:** put `NVIDIA_API_KEY=nvapi-...` in `~/.codecouncil/env`
(outside any git repo — never committed) and the critic uses NVIDIA's hosted
Nemotron for free automatically, via the bundled provider extension
(`critic/pi_extensions/nvidia_provider.mjs`). Override the model with
`COUNCIL_MODEL=nvidia-nim/nvidia/<model-id>`.
Output: every verdict (PASS and suggestions) appends to
`.codecouncil/suggestions.ndjsonl` tagged with `beat` + `heuristics_version` —
the metrics substrate for the Reflector. Heuristics seed:
`critic/heuristics.seed.md`, copied to `.codecouncil/heuristics.md` on first run.
Set `CRITIC_CMD=<script>` to stub the model in tests.

## Council mode

By default the Critic asks one model per batch. Council mode adds a second,
independent model — a **prober** — asked the exact same prompt, so a batch
gets two takes instead of one.

The pairing is deliberate, not arbitrary: a small bake-off
(`docs/benchmarks/2026-07-21-critic-bakeoff.json`,
`docs/benchmarks/2026-07-21-critic-bakeoff-round2.json`) measured two very
different profiles on the same 7 frozen eval cases (3 clean, 4 genuinely
flaggable):

- **NVIDIA Nemotron** (the default primary) — 0 false positives on clean
  changes, but only 2-of-4 catches on the flaggable ones. A precision anchor.
- **OpenRouter `openai/gpt-5-mini`** — 4-of-4 catches, but 2 false positives
  on clean changes. Full recall, noisier.

**Merge rule** (`critic/main.py`'s `merge_council`): the primary's verdict
flows through whenever it has one — if the primary flags something, that's
the finding, prober agreement or not. Only when the primary says PASS but the
prober says SUGGESTION does the prober's finding get a chance, and even then
only after `critic/verify.py` reproduces it — an unverified prober-only
finding is exactly the false-positive failure mode the bake-off measured, so
it never ships without repro proof. Every judged batch records which model(s)
produced the verdict as a `council` field (`prober_verdict`, `agreement`,
`prober_model`) on the suggestion row.

**Enable it:**

```sh
python3 -m codecouncil /path/to/repo --prober openrouter/openai/gpt-5-mini
python3 -m critic /path/to/repo --prober openrouter/openai/gpt-5-mini
# or set COUNCIL_PROBER=openrouter/openai/gpt-5-mini in the environment
```

Precedence is `--prober` flag > `COUNCIL_PROBER` env > off (unchanged
single-model behavior — no `council` key is ever added to a suggestion row
unless a prober is configured). The launcher's preflight warns if a
configured `openrouter/*` prober has no `OPENROUTER_API_KEY` available (env
or `~/.codecouncil/env`), the same way it already warns about a missing
primary-model key.

**Cost:** one extra model call per judged batch — calls are already gated on
actual code changes (never on reasoning-only beats), so this is roughly ~1¢
per judged batch at `gpt-5-mini` pricing, not per beat.

## Install the hook (per watched repo)

```sh
python3 -m hooks.install /path/to/repo-being-coded-in   # idempotent settings.json merge
```

The hook is fail-open (any error → silent exit 0), delivers each suggestion at
most once per channel (`.codecouncil/delivered.json`), never blocks twice, and
ignores suggestions older than 10 minutes.

## Run the Reflector

```sh
python3 -m reflector /path/to/repo-being-coded-in     # grade + maybe rewrite, every 5 min
python3 -m reflector . --once --force-rewrite         # demo: rewrite below threshold
python3 -m reflector.report .                         # acceptance per heuristics version
```

Rewrites are guarded: strict `version: N+1` + length validation, then an eval
gate — the candidate must match or beat the current rules on the frozen cases
(`evals/cases/` + self-harvested `evals/cases-harvested/`) or it's rejected.
Prior versions archive to `.codecouncil/heuristics-history/` (atomic swap), and
a version whose real-world acceptance drops below its predecessor's is
auto-rolled-back — as a new, higher version, so history never rewinds.

## Run the dashboard

```sh
cd ui && npm install && npm run dev        # http://localhost:4700
COUNCIL_REPO=/path/to/repo npm run dev     # watch a different repo's .codecouncil/
```

xai-inspired live UI: reads the real `.codecouncil/` files every 2s — observer
activity feed, every Critic verdict + suggestion with its Reflector grade, and
the acceptance-per-heuristics-version curve (same math as `reflector.report`,
n-counts shown). Nothing is mocked; empty states explain what unlocks them.

## Tests

```sh
python3 -m unittest discover -s tests
```

CI runs the same suite on every push and pull request (see `.github/workflows/ci.yml`).

Design notes: `docs/specs/2026-07-18-observer-design.md`.
