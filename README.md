# CodeCouncil

An AI peer programmer that watches your Claude Code session and, over time, gets
measurably better at critiquing your work. Three loops:

1. **Observer** (done) — heartbeat daemon pairing the agent's *intent* (transcript
   reasoning + tool calls) with what actually *changed* (git diff).
2. **Critic** (done) — reads observations each beat, mostly says PASS, occasionally
   flags one high-value issue. Judgment runs as an OpenClaw agent in a NemoClaw
   sandbox (`nemoclaw codecouncil agent --agent critic`, Nemotron via routed
   inference — the Critic holds no API key).
3. **Reflector** — rewrites the Critic's own heuristics based on which suggestions
   landed. The recursive self-improvement loop.

## Run the Observer

```sh
python3 -m observer /path/to/repo-being-coded-in   # 30s heartbeat
python3 -m observer . --once                       # single beat
python3 -m observer . --from-start                 # replay transcripts from the top
```

Output: live terminal narration + `.codecouncil/observations.ndjsonl` in the watched
repo (one JSON event per line: `reasoning`, `tool_call`, `diff`). No dependencies —
Python 3.10+ stdlib only.

## Run the Critic

```sh
python3 -m critic /path/to/repo-being-coded-in    # 30s heartbeat, call only when new material
python3 -m critic . --once                        # single beat
```

Requires the `codecouncil` NemoClaw sandbox with a `critic` OpenClaw agent
(persona: `critic/AGENTS.sandbox.md`, uploaded to `/sandbox/workspaces/critic/AGENTS.md`).
Output: every verdict (PASS and suggestions) appends to
`.codecouncil/suggestions.ndjsonl` tagged with `beat` + `heuristics_version` —
the metrics substrate for the Reflector. Heuristics seed:
`critic/heuristics.seed.md`, copied to `.codecouncil/heuristics.md` on first run.
Set `CRITIC_CMD=<script>` to stub the sandbox in tests.

## Tests

```sh
python3 -m unittest discover -s tests
```

Design notes: `docs/specs/2026-07-18-observer-design.md`.
