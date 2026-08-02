# Changelog

Notable changes. Format: [Keep a Changelog](https://keepachangelog.com); versioning: pre-1.0, minor bumps may break.

## [Unreleased]

### Added
- Per-key model auto-defaults: with no model configured, the first configured
  API key picks its provider's default model (`core.config.KEY_DEFAULT_MODELS`,
  ordered free NVIDIA first, Anthropic last for decorrelation) — `/keys` alone
  is now a working setup for every supported provider
- `/model` with no argument shows the current resolved model, which layer set
  it (flag/env/config/auto), and copy-pasteable examples for configured keys
- `/model`/`/prober` validation at set time: warns (never blocks) on a missing
  provider key, malformed `provider/model-id`, or single-segment
  `openrouter/...`/`nvidia-nim/...` ids that would 404
- `/keys` now chains into model choice: reports the model the critic will use,
  or offers the saved key's default when the current model runs on another key

### Fixed
- `/model` and `/prober` no longer silently lose to a launch-time `--model`
  flag or exported `COUNCIL_MODEL`/`COUNCIL_PROBER` on critic restart — a
  console-set knob resolves from config.json only

## [0.1.0] — 2026-07-30

First public release. An independent, execution-grounded reviewer for AI
coding agents — a different model that runs a repro against your code to prove
a finding, in-session, local, and free.

- Observer: event-driven transcript + git watching, redaction at capture
- Critic: findings verified by an executed repro before delivery (never a
  bare model opinion); mechanical screening for documented AI-code failure
  modes (injection patterns, unsafe deserialization, hallucinated/typo'd
  imports, weakened tests); opt-in council mode with a decorrelated prober;
  opt-in property probes; session receipts on "done"
- Hooks: findings delivered into the coding agent's own context, fail-open;
  `COUNCIL-REBUTTAL:` protocol; opt-in done-gate holds a "done" until the
  critic finishes judging
- Reflector: graded outcomes, eval-gated heuristics rewrites with
  auto-rollback, rebuttal → knowledge distillation
- Benchmark harness: with/without/naive arms, execute-the-exploit safety tier,
  scorer self-test — every run published, ties included (docs/benchmarks/)
- Community health: Code of Conduct (Contributor Covenant), SUPPORT.md,
  AGENTS.md (agent-contributor guide), issue/PR templates, security policy
- One-command installer, interactive console (`/keys`, `/model`, `/status`),
  signal-first terminal, React dashboard
