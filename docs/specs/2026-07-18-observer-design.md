# Observer — design (2026-07-18)

First of three loops in CodeCouncil, an AI peer programmer that watches a Claude Code
session and (later) critiques the work and improves its own heuristics.

## What it does

`python -m observer /path/to/watched/repo --interval 30`

Every heartbeat:

1. Tail new lines from all session JSONLs in `~/.claude/projects/<munged-repo-path>/`.
2. Capture `git diff HEAD` + untracked files in the watched repo; only note it when changed.
3. Emit events to `.codecouncil/observations.ndjsonl` (the Critic's future input) and a
   live terminal summary.

## Decisions

- **Python 3, stdlib only.** No deps to break at a hackathon.
- **Polling, not fs-watch.** Dumber, more reliable.
- **Byte-offset bookmarks** per JSONL file, persisted in `.codecouncil/state.json` in the
  watched repo. Restart-safe; only complete lines (up to last `\n`) are consumed.
- **Project dir discovery:** try the munged path first (`/` etc. → `-`), verify via the
  `cwd` field inside the JSONL; fall back to scanning all project dirs for a matching `cwd`.
- **Extraction:** from `type: "assistant"` lines only, skipping `isSidechain: true`:
  `thinking`/`text` blocks → `reasoning` events (truncated), `tool_use` blocks →
  `tool_call` events (tool name + salient inputs).
- **Event schema:** `{"ts", "beat", "session", "type": "reasoning|tool_call|diff", "payload"}`.

## Out of scope (this step)

Hooks, Cursor/Codex support, model calls, debounce beyond heartbeat batching.
