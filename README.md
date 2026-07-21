# CodeCouncil

An AI peer programmer that watches your Claude Code session and, over time, gets
measurably better at critiquing your work. Three loops:

1. **Observer** (done) — heartbeat daemon pairing the agent's *intent* (transcript
   reasoning + tool calls) with what actually *changed* (git diff).
2. **Critic** (done) — reads observations each beat, mostly says PASS, occasionally
   flags one high-value issue. Judgment runs as a headless [pi](https://pi.dev)
   agent turn (`pi -p`) — any provider pi supports, including OpenAI-compatible
   endpoints and local models.
3. **Hook injection** (done) — Claude Code hooks that deliver Critic suggestions
   into the coding agent's own context: medium/high injected after edits
   (PostToolUse), high blocks completion once (Stop) until fixed or rebutted.
4. **Reflector** (done) — grades delivered suggestions against what actually
   happened next (accepted / rebutted / ignored, model-judged from post-delivery
   diffs + reasoning), then rewrites the Critic's heuristics from the grades.
   The recursive self-improvement loop.

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
python3 -m critic /path/to/repo-being-coded-in    # 30s heartbeat, call only when new material
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
Set `CRITIC_CMD=<script>` to stub the sandbox in tests.

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

Rewrites are guarded: strict `version: N+1` + length validation (bad output →
old file kept), prior versions archived to `.codecouncil/heuristics-history/`,
atomic swap so the Critic never reads a half-written file.

## Run the dashboard

```sh
cd ui && bun install && bun dev        # http://localhost:4700
COUNCIL_REPO=/path/to/repo bun dev     # watch a different repo's .codecouncil/
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
