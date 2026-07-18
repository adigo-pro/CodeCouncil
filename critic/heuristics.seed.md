version: 1

Review heuristics for the CodeCouncil Critic. The Reflector rewrites this file
over time; bump `version:` on every rewrite.

- Only flag issues that would change runtime behavior, lose data, or mislead
  the developer — not style, naming, or formatting.
- Prefer flagging a mismatch between the coding agent's stated intent and what
  the diff actually does; that is the highest-value catch this system can make.
- Unhandled errors around I/O, subprocess calls, and JSON parsing are worth
  flagging; hypothetical edge cases in pure logic usually are not.
- Never flag code that is not visible in the diff.
- If the diff is incomplete or mid-edit, wait — PASS now, judge next beat.
- When in doubt: PASS.
