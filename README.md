# CodeCouncil

An AI peer programmer that watches your Claude Code session and, over time, gets
measurably better at critiquing your work. Three loops:

1. **Observer** (done) — heartbeat daemon pairing the agent's *intent* (transcript
   reasoning + tool calls) with what actually *changed* (git diff).
2. **Critic** (done) — reads observations each beat, mostly says PASS, occasionally
   flags one high-value issue. Judgment runs as an OpenClaw agent in a NemoClaw
   sandbox (`nemoclaw codecouncil agent --agent critic`, Nemotron via routed
   inference — the Critic holds no API key).
3. **Hook injection** (done) — Claude Code hooks that deliver Critic suggestions
   into the coding agent's own context: medium/high injected after edits
   (PostToolUse), high blocks completion once (Stop) until fixed or rebutted.
4. **Reflector** (done) — grades delivered suggestions against what actually
   happened next (accepted / rebutted / ignored, judged by an OpenClaw agent from
   post-delivery diffs + reasoning), then rewrites the Critic's heuristics from
   the grades. The recursive self-improvement loop.

## Run the Observer

```sh
python3 -m observer /path/to/repo-being-coded-in   # 30s heartbeat
python3 -m observer . --once                       # single beat
python3 -m observer . --from-start                 # replay transcripts from the top
```

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

Requires the `codecouncil` NemoClaw sandbox with a `critic` OpenClaw agent
(persona: `critic/AGENTS.sandbox.md`, uploaded to `/sandbox/workspaces/critic/AGENTS.md`).
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

## Tests

```sh
python3 -m unittest discover -s tests
```

Design notes: `docs/specs/2026-07-18-observer-design.md`.
