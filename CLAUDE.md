# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CodeCouncil is an AI peer reviewer that watches an AI coding agent (Claude Code) work in real time, flags genuine issues, verifies findings by running code in a sandbox before speaking, and rewrites its own review heuristics based on which suggestions turned out to be right. Plain-language overview: `docs/PROJECT_GUIDE.md`.

## Commands

```sh
python3 -m unittest discover -s tests                    # full suite
python3 -m unittest tests.test_critic                    # one module
python3 -m unittest tests.test_observer.TestTailing      # one class

python3 -m codecouncil /path/to/repo     # everything: hooks + observer + critic + reflector
python3 -m observer /path/to/repo        # observer daemon (--once, --from-start, --interval N)
python3 -m critic /path/to/repo          # critic daemon (--once, --judge-every-beat)
python3 -m reflector /path/to/repo       # reflector daemon (--once, --force-rewrite)
python3 -m reflector.report /path/to/repo  # acceptance per heuristics version
python3 -m hooks.install /path/to/repo   # install Claude Code hooks (idempotent)
python3 -m training.run                  # scripted headless sessions that generate real data
python3 -m evals.run /path/to/repo       # replay frozen cases against every heuristics version

cd ui && npm run dev                     # dashboard at http://localhost:4700 (COUNCIL_REPO=/path to watch another repo)
```

Python is stdlib-only by design (3.10+): do not add pip dependencies to observer/critic/reflector/hooks/training/evals. The dashboard (`ui/`) is React + Vite + Tailwind.

## Architecture

Four loops communicating **only through NDJSON/JSON files** in the watched repo's `.codecouncil/` directory (gitignored). Each loop is an independent daemon; there are no imports across loop boundaries except small shared utilities (`core.store` for NDJSON + startup waits, `observer.events`, `observer.transcript`, `critic.agent`). `codecouncil/` is only a launcher: it installs hooks and runs the three loops as subprocesses with prefixed output. CodeCouncil watches its own repo — `.codecouncil/` here contains live data, and the hooks are installed on this repo, so the Critic may review your work as you code.

1. **Observer** (`observer/`, event-driven: beats fire when a transcript grows, `--interval` is only the fallback floor) — pairs *intent* with *reality*. Tails Claude Code session transcripts (`~/.claude/projects/<munged-path>/*.jsonl`, persisted byte offsets in `state.json`) into `reasoning`/`tool_call` events, and snapshots git state into `diff` events (fingerprinted, emitted only on change; includes capped contents of new untracked files) and `commit` events (`old..new` HEAD ranges). Appends to `observations.ndjsonl`.

2. **Critic** (`critic/`, ~10s beat) — reads new observations; when code actually changed (with a floor between model calls), sends one prompt to a headless pi agent turn asking for PASS-with-reason or ONE suggestion. Findings are first **verified** by running a repro (`critic/verify.py`) — refuted findings are never delivered. Also runs a task review when the agent declares work done (are the completion claims supported? did a test command actually run?). Every verdict + its exact prompt goes to `suggestions.ndjsonl`/`prompts/`, tagged with `heuristics_version`. Model calls run on a worker thread so the beat never blocks.

3. **Hooks** (`hooks/`) — delivery channel into the coding agent's own context via Claude Code hooks: PostToolUse injects medium/high suggestions after edits; Stop blocks a "done" declaration once for high severity until fixed or rebutted. `peer_hook.py` must **fail open** (any error → silent exit 0) — preserve that invariant. Delivery/dedup state lives in `delivered.json` (`hooks/ledger.py`); decision rules in `hooks/logic.py` (pure, no I/O — keep it that way for tests).

4. **Reflector** (`reflector/`, slow beat) — grades each delivered suggestion from post-delivery diffs + reasoning (`accepted`/`rebutted`/`ignored`, model-judged with a deterministic did-the-file-change cross-check; a `COUNCIL-REBUTTAL: <reason>` line from the coding agent — which the hook text invites — grades `rebutted` deterministically, no model call) into `outcomes.ndjsonl`, then rewrites `heuristics.md` from the grades. Rewrites are guarded: strict `version: N+1` + length validation, prior versions archived to `heuristics-history/`, atomic swap. The Critic reads `heuristics.md` on every call — that file is the thing being self-improved.

**Model boundary:** all model calls go through `critic/agent.py` — one non-interactive [pi](https://pi.dev) turn (`pi -p --no-session --no-tools …`, persona via `--system-prompt`). Judgment turns get no tools; verification turns get `read,bash` in a throwaway staging directory (`critic/verify.py`) so repros never touch the watched repo. `COUNCIL_MODEL=provider/model` overrides pi's default; `PI_BIN` overrides the executable. Set `CRITIC_CMD=<executable>` to stub the model in tests: it runs as `$CRITIC_CMD <prompt-file>`, stdout is the reply. Personas live in `critic/persona.md` and `reflector/persona.md`.

`critic/agent.py` also auto-loads `~/.codecouncil/env` (outside any watched repo — a credential placed there can never be committed regardless of which repo CodeCouncil is pointed at) to top up the subprocess environment, and always attaches `critic/pi_extensions/nvidia_provider.mjs` via `pi -e`. If `NVIDIA_API_KEY` resolves (real env or that file) and `COUNCIL_MODEL` is unset, the default becomes NVIDIA-hosted Nemotron (`nvidia-nim/nvidia/nemotron-3-super-120b-a12b`) — zero pi login required. Model `id`s in that extension must be NVIDIA's full catalog string (e.g. `nvidia/nemotron-3-super-120b-a12b`); pi's `openai-completions` provider sends `model.id` verbatim as the request's `model` field, so a shorter id 404s.

**Measurement:** two independent signals of self-improvement — acceptance rate per heuristics version (`reflector/report.py`, mirrored exactly by `ui/server/council.ts` so the dashboard can't diverge from the real metric) and frozen eval cases (`evals/cases/*.json`) replayed against every heuristics version.

## Conventions

- Tests are stdlib `unittest`, one file per loop in `tests/`; anything touching model calls stubs the sandbox via `CRITIC_CMD`. Fixtures include a real session transcript (`tests/fixtures/session.jsonl`).
- All NDJSON readers must tolerate a partial trailing line (files are appended mid-write) and skip unparseable lines rather than crash. `observations.ndjsonl` grows unbounded over a session, so whole-file consumers use `core.store.read_tail_rows` (and the UI server's `readNdjsonTail`) — the critic hot path is already O(new bytes) via byte offsets. Only reach back a bounded window; never re-parse the whole log per cycle.
- Daemons never die on missing inputs — they wait; state files that fail to parse are discarded and rebuilt, not fatal.
- Long text going into events/prompts is always truncated with an explicit `… [N chars total]` marker; caps are module-level constants.
