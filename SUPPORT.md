# Support

Thanks for using CodeCouncil. Here's where to go, depending on what you need:

## I have a question / want to discuss an idea

Open a [GitHub Discussion](https://github.com/adigo-pro/CodeCouncil/discussions)
(if enabled) or a [feature-request issue](https://github.com/adigo-pro/CodeCouncil/issues/new?template=feature_request.md).
Questions about how a loop works, how to configure a model provider, or whether
a use case is a good fit are all welcome.

## Something is broken

Open a [bug report](https://github.com/adigo-pro/CodeCouncil/issues/new?template=bug_report.md).
The template asks for the relevant terminal lines and — if a finding is
involved — the matching row from `.codecouncil/suggestions.ndjsonl` and the
prompt file it names. Redaction runs at capture, but eyeball before pasting.

## I found a security issue

See [SECURITY.md](SECURITY.md) — open a private
[security advisory](https://github.com/adigo-pro/CodeCouncil/security/advisories/new)
rather than a public issue.

## I want to contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) — it lists the eight invariants that
matter more than any style guide, the zero-setup dev loop, and good first
issues.

## First things to check

- **The dashboard** (`cd ui && npm install && npm run dev` → localhost:4700)
  shows everything the council is doing, including its acceptance rate and any
  malformed-reply badge.
- **`/status`** in the running council prints daemons, beats, the last verdict,
  the heuristics version, and which keys are available.
- **No findings appearing?** Confirm a model key resolves (`/config`), that the
  hooks installed (`.claude/settings.json` in the watched repo), and that the
  coding agent's session is actually being tailed (the observer needs a Claude
  Code transcript for that repo).

This is a young project — the self-improvement curves are days old, not months.
Honest bug reports and rebuttals (`COUNCIL-REBUTTAL: <reason>`) are how it gets
better.
