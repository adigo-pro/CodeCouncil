# Changelog

Notable changes. Format: [Keep a Changelog](https://keepachangelog.com); versioning: pre-1.0, minor bumps may break.

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
