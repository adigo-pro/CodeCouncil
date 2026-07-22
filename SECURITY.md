# Security Model

CodeCouncil reads your coding agent's session transcripts and your repo's git
state, and sends review prompts to a model provider you configure. You should
know exactly what leaves your machine and what never does. This document is
that contract — and because the code is open, every claim here is auditable.

## What leaves your machine

One thing only: **review prompts**, sent to the model provider(s) you
configure (NVIDIA-hosted, OpenRouter, or anything [pi](https://pi.dev)
supports — your choice, your keys). A prompt can contain:

- excerpts of the coding agent's reasoning and tool calls (truncated, redacted)
- git diffs and capped contents of new/changed files (redacted)
- commit subjects and diffs (redacted)
- your repo's README/CLAUDE.md excerpts and the learned heuristics/knowledge files

If you configure a local model through pi, nothing leaves at all.

## Redaction at capture

Every text field is passed through `core/redact.py` **at observer capture
time** — before it is written to disk, before any prompt is built. Credential
shapes (AWS keys, API tokens, private-key blocks, high-entropy assignments to
secret-like names) become `«REDACTED:kind»` markers. The marker itself is
taught to the critic as a confirmed secret-in-code finding, so secrets are
*caught* without ever being *transmitted*. Model-authored text that gets
persisted (issues, rationales, repro commands, distilled facts) is redacted
and capped again at parse time.

Redaction is pattern-based and deliberately precision-first; it is a strong
floor, not a guarantee against every exotic secret format. Review
`core/redact.py` for the exact patterns.

## What never leaves

- Your API keys. They live in `~/.codecouncil/env` — **outside every
  repository**, so they can never be committed regardless of which repo
  CodeCouncil watches. Keys are read at request time and passed to pi as
  environment variables; CodeCouncil never persists them elsewhere.
- Raw repository history, untouched files, or anything outside the observed
  diff/transcript window.

## Execution boundaries

- **Verification repros** run against a *copy* of the flagged file in a
  throwaway temp directory — never in your repo.
- **Investigation tools** on judgment turns are custom, path-jailed
  implementations (`critic/pi_extensions/jail.mjs`): reads are confined to
  the repo root (symlink-escape and traversal rejected, `.git`/`.codecouncil`
  excluded). pi's builtin file tools are deliberately NOT used for this,
  because they resolve `~` and absolute paths.
- **Repro commands** delivered to your coding agent are allowlist-gated
  (`python3`/`pytest`/… prefixes, shell metacharacters rejected) and framed
  "review before running" — they are suggestions as text, never executed by
  CodeCouncil itself.
- The Claude Code hook (`hooks/peer_hook.py`) is **fail-open**: any internal
  error exits silently rather than breaking your session.

## Reporting a vulnerability

Open a GitHub security advisory on this repository (Security → Advisories →
Report a vulnerability), or open an issue if the report is not sensitive.
The redaction layer, the path jail, and the repro-command gate are the
highest-value targets — reports there are especially welcome.
