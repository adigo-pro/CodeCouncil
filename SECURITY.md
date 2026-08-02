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

Two related boundaries:

- **Capture never follows a symlink out of the repo.** `git ls-files` lists
  untracked symlinks and a naive read would follow one to, say,
  `~/.aws/credentials` or a sibling checkout — capturing a file that is not
  part of your project and shipping it in the next prompt. `observer/gitwatch.py`
  resolves each path and refuses anything landing outside the repo root, the
  same containment the judgment-turn tools already enforce.
- **Model-authored text is control-character stripped**, not just redacted
  (`core/redact.py`'s `sanitize`). Findings are printed to your terminal and
  injected into your coding agent's context, so an ANSI escape sequence in a
  model-written `issue` string could otherwise repaint the line above it and
  misrepresent a finding's severity.

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
- **Repros** delivered to your coding agent are the verification script
  itself, redacted, control-character-stripped, capped, and framed "review
  before running". CodeCouncil hands it over as *text* and never executes it
  in your repo — but it is model-authored code, so treat it as a suggestion
  to read, not a command to run blind. (Earlier versions of this document
  described a `python3`/`pytest` prefix allowlist; that gate applied to the
  single-shell-command repro format which no longer exists.)
- The Claude Code hook (`hooks/peer_hook.py`) is **fail-open**: any internal
  error exits silently rather than breaking your session.

## Model-authored code execution

The Critic's verification (`critic/verify.py`) and opt-in probe
(`critic/probe.py`) features ask the model to write a short repro/probe
script, which CodeCouncil then **executes on your machine** — in a
throwaway staging directory, never in your repo — to prove a finding real
before it's ever delivered.

That execution gets two independent layers (`core/sandbox.py`):

1. **A scrubbed environment.** The child's environment is a minimal
   allowlist built from scratch (`PATH`, `HOME`, `LANG`/`LC_ALL`, plus
   `PYTHONPATH` pointed at the staging copy), never a copy of the parent's.
   No API key is in it, and `HOME` points inside the staging directory.
2. **An OS sandbox.** On macOS via `sandbox-exec`, on Linux via `bwrap`:
   **all network egress is denied**, and **reads under your real home
   directory are denied** (so `~/.codecouncil/env`, `~/.ssh`, and your shell
   history are unreachable). The staging directory stays writable, and the
   Python interpreter's own prefixes stay readable — necessary because
   pyenv/asdf install the interpreter *inside* your home.

Layer 2 is not redundant, and this is worth being precise about because an
earlier version of this document got it wrong. It claimed layer 1 alone
meant "model-authored code cannot read your keys." **That was false.**
`HOME` only governs `~` expansion; `pwd.getpwuid(os.getuid()).pw_dir`
returns your real home regardless, and reading `<real home>/.codecouncil/env`
by absolute path and POSTing it out was demonstrated working. Environment
scrubbing cannot fix that — `getpwuid` reads the OS user database, not the
environment — which is why the OS boundary was added.

**Scope of the guarantee.** The two headline risks (credential theft and
network exfiltration) are closed where a sandbox mechanism exists. It is
still not a full syscall jail: on macOS the profile denies network and home
reads over an `(allow default)` base, so a script can read world-readable
paths elsewhere on disk — with egress denied, its only channel back is
stdout, which CodeCouncil redacts and caps.

**If no sandbox mechanism exists** (a Linux host without `bwrap`), scripts
run with layer 1 only and CodeCouncil prints a warning rather than implying
protection it isn't providing. Set `COUNCIL_SANDBOX=require` (or
`"sandbox": "require"` in `~/.codecouncil/config.json`) to refuse to execute
instead; `off` disables sandboxing for debugging.

Even so: run CodeCouncil on repositories you would be willing to execute
code from. Verification and probe scripts **import the file under review**,
and importing a Python module runs its top-level statements — so "review
this repo" does mean "run some of this repo's code", sandboxed.

## Reporting a vulnerability

Open a GitHub security advisory on this repository (Security → Advisories →
Report a vulnerability), or open an issue if the report is not sensitive.
The redaction layer, the path jail, and the repro-command gate are the
highest-value targets — reports there are especially welcome.
