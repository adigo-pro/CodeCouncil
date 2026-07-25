# Show HN draft

> **Title:** Show HN: CodeCouncil – an AI reviewer that caught a security bug in its own code
>
> (fallback titles, in preference order:)
> - Show HN: An AI peer reviewer for AI coding agents – findings ship with executed proof
> - Show HN: CodeCouncil – it reviews Claude Code while it works, and it reviews itself

---

Hi HN — I built CodeCouncil, an open-source AI peer reviewer that watches a
coding agent (Claude Code today) *while it works*, and I want to lead with
the thing that convinced me it's real:

**While building CodeCouncil, CodeCouncil reviewed CodeCouncil — and caught
real bugs in itself.** A secret-leak bug in its own redaction code that two
independent reviewers had approved. A permissions hole in its own installer
(delivered with an executed repro, blocking my "done" until I fixed it). A
probe-dedup flaw that would have silently suppressed findings. Each catch
came through the exact mechanism it sells.

**What it does differently from PR-review bots (CodeRabbit, Greptile):**

- **It reviews during the session, not at the PR.** By the time a PR comment
  arrives, the agent that wrote the code is gone — its context evicted.
  CodeCouncil delivers findings *into the agent's own context* via hooks,
  while the agent still holds the code in its head. The agent fixes it (or
  formally rebuts it) in-session.
- **Findings arrive with receipts.** Before delivering, the critic writes and
  runs a repro against a staged copy. Refuted findings are never delivered.
  For security findings it goes further: it executes the exploit.
- **It reads intent, not just diffs.** The most common agent failure isn't
  broken syntax — it's a claim that isn't true ("tests pass" — they never
  ran). CodeCouncil pairs the agent's stated reasoning with what actually
  changed, and writes a claims-vs-verified receipt when the agent says done.
- **It grades itself and rewrites its own review rules** — eval-gated,
  auto-rolled-back on regression. Every finding cites the rule that
  motivated it; rules that don't earn acceptance die.

**Honest numbers, because that's the whole brand:**

- We built a ponytail-style benchmark (control arms, execute-the-exploit
  safety scoring, contamination-proof isolation, scorer self-tests — their
  methodology, credited) and ran 45 real sessions. **The arms tied — and we
  published exactly why:** the surgical tasks finish in ~14s, faster than
  any asynchronous reviewer loop can deliver a verdict, so the with-council
  arm was mechanically a placebo (0 findings delivered in-session, provable
  from the committed raw rows). A tie you can explain beats a win you
  can't. The fix — a synchronous done-gate — is top of the roadmap, and
  run 2 happens after it ships.
- Same honesty on the earlier feature-tier pilot: **no measurable difference
  on short easy tasks** (ceiling effect) — published.
- A reviewer *adds* cost and latency. We will never claim "cheaper" or
  "faster." The claim is: fewer untrue claims and fewer shipped
  vulnerabilities, and the cost of that is reported openly.

**Stack:** stdlib-only Python (no pip installs), four independent daemons
communicating through append-only NDJSON files, models via pi (free NVIDIA
Nemotron key works out of the box — no card). Apache-2.0.

    curl -fsSL https://raw.githubusercontent.com/adigo-tamu/CodeCouncil/main/install.sh | sh

Site: https://codecouncil.vercel.app · Repo: https://github.com/adigo-tamu/CodeCouncil

Things I know it doesn't do yet: PR mode, non-Claude-Code agents (the
observer only needs a transcript stream — adapters welcome), and the
self-improvement curves are days old, not months. The dashboard shows
everything it does and everything it gets wrong — acceptance rate included.

I'll be in the thread all day.
