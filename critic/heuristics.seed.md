version: 1

Review heuristics for the CodeCouncil Critic. The Reflector rewrites this file
over time; bump `version:` on every rewrite.

- Only flag issues that would change runtime behavior, lose data, mislead the
  developer, or hurt them later (leaked secrets, unportable config) — never
  style, naming, or formatting.
- Highest value: mismatches between the coding agent's stated intent — claims
  in reasoning, comments, or commit messages — and what the diff actually
  does. "Handled X" with no X in the diff is the signature catch.
- A claim that tests pass when no test command appears in the tool calls is
  always worth flagging.
- Flag credentials, API keys, or tokens appearing in code, config, commands,
  or commit contents — even in files that look private.
- A `«REDACTED:kind»` marker in the material is a confirmed secret-in-code
  finding, not a maybe: the capture layer already matched a high-confidence
  credential shape and stripped the value before it reached you. Flag it as
  high severity on sight, even though the value itself is never visible.
- Flag machine-specific absolute paths, usernames, or hostnames written into
  committed code or config; they break on every other machine.
- Comment or docstring promises one behavior, code does another: flag it.
- Unhandled errors around I/O, subprocess calls, and JSON parsing are worth
  flagging; hypothetical edge cases in pure logic usually are not.
- Pre-existing problems visible in or immediately around the changed code are
  fair game — a good peer mentions the leaked token or the lying docstring
  sitting right next to the line being edited, even if this change didn't
  introduce it.
- Before flagging "X is missing", look for X with your tools; flag only if it
  is genuinely absent.
- Never flag code that is not visible in the provided material.
- If the diff is incomplete or mid-edit, wait — PASS now, judge next beat.
- A justification for a shortcut in the agent's reasoning ("for simplicity",
  "this is fine because") immediately before the shortcut lands is worth
  reading twice — check the shortcut, not the justification.
- A new or changed test that asserts the current implementation's exact
  behavior deserves a check: does it encode intent, or encode the bug?
- When in doubt: PASS.
