# Show HN draft

> **Title:** Show HN: Claude 5 verifies its own code now. Here's why a different model still catches more
>
> (fallback titles, in preference order:)
> - Show HN: CodeCouncil – an independent, execution-grounded reviewer for AI coding agents
> - Show HN: An AI reviewer that caught a security bug in its own code (open source)

---

Hi HN — Anthropic shipped Claude 5 last week with self-verification as the
headline: the model runs its own tests before it says "done." That's real
progress. It also sharpened the reason I built CodeCouncil, so let me lead
with the tension instead of dodging it.

A model checking its own work is the *most correlated reviewer possible* — same
weights, same context, same incentive to be finished. The 2026 research on
this is blunt: self-verification makes a model **more convincing, not more
correct** ("More Convincing, Not More Correct", arXiv 2607.05904 — a
reference-free self-judge's apparent pass rate climbs to 0.94 while true
accuracy sits at 0.20; the errors even transfer across model families). "All
tests pass" becomes a better-*disguised* false claim, not a rarer one. The
documented antidote isn't more judges — it's an independent checker that
**executes** instead of judging, ideally trained differently so its blind
spots don't line up with the author's.

That's CodeCouncil: an open-source reviewer that watches your Claude Code
session in real time and delivers a finding only after **running a repro
against your code** — ground truth, not another model's opinion — using a
*different* model (free NVIDIA Nemotron by default) with no stake in calling
your task done. Full argument with citations:
github.com/adigo-tamu/CodeCouncil/blob/main/docs/benchmarks/WHY.md

**The proof I trust most is that it caught bugs in itself.** While I was
building it, CodeCouncil reviewed CodeCouncil — a secret-leak bug in its own
redaction code that two reviewers had approved; a permissions hole in its own
installer (delivered with an executed repro, blocking my "done" until I fixed
it); a probe-dedup flaw that would have silently suppressed findings. Each
came through the exact mechanism it sells.

**How it differs from what's out there** (including Claude Code Review, which
is a fine PR-time enterprise product — $15–25/review, runs on Anthropic's
cloud, GitHub-only, Claude reviewing Claude):

- **It executes, it doesn't judge.** Before delivering, the critic writes and
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
