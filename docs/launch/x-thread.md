# X / Twitter launch thread draft

**1/**
I built an AI code reviewer that watches Claude Code work in real time.

While I was building it, it reviewed its own code.

It caught a security bug in itself. With an executed proof. And blocked me
from saying "done" until I fixed it.

Open source. Here's how it works 🧵

**2/**
The problem: AI agents' most common failure isn't broken syntax.

It's a claim that isn't true.

"Handled the edge case" (it didn't). "All tests pass" (they never ran).
Research backs it: 45% of AI code ships OWASP vulnerabilities while syntax
is >95% correct. Nobody reads the diff to check.

**3/**
PR bots (CodeRabbit, Greptile) review too late for agents.

By the time a PR comment lands, the agent that wrote the code is *gone* —
its context evicted. Every finding costs a cold restart.

CodeCouncil reviews in-session and delivers findings INTO the agent's own
context. It fixes — or formally rebuts — while the code is still in its head.

**4/**
Findings arrive with receipts.

Before delivering anything, the critic writes and runs a repro against a
staged copy of your code. Refuted findings are never delivered.

Security findings go further: it executes the exploit. Not "this looks like
SQL injection" — here's the injection, running.

[attach: demo GIF]

**5/**
It grades itself.

Every suggestion is graded against what you actually did next. Every PASS
is graded too — if a fix commit later revises files a PASS covered, that
silence is graded "missed" and becomes a frozen eval case.

Then it rewrites its own review rules. Eval-gated. Auto-rolled-back on
regression.

**6/**
The receipts (dogfooding, all in the repo):

- caught a secret-leak bug in its own redaction code
- caught a permissions hole in its own installer (executed repro)
- caught a lying docstring in its own miss-detector

Each one through the exact mechanism it sells.

**7/**
And the anti-hype part: we built a rigorous with-vs-without benchmark
(control arms, executed exploits, scorer self-tests) and the arms TIED.

We published it, with the raw rows and the mechanistic reason: the tasks
finish faster than any async reviewer can speak. The fix (a synchronous
done-gate) is now the roadmap. A reviewer adds cost — we'll never claim
"cheaper/faster." The claim is safety + truth, priced openly.

**8/**
Free to run: stdlib-only Python, no pip installs, and a free NVIDIA Nemotron
key drives the critic (no card needed). Any provider works (OpenAI,
Anthropic, Google, Groq, OpenRouter).

One command:
curl -fsSL https://raw.githubusercontent.com/adigo-tamu/CodeCouncil/main/install.sh | sh

**9/**
Apache-2.0. The observer only needs a transcript stream — adapters for
Cursor/Codex/etc. are the most-wanted contribution.

Site: codecouncil.vercel.app
Repo: github.com/adigo-tamu/CodeCouncil

If your agent says "done," make it prove it. ⭐

---
*Posting notes: attach demo.gif to tweet 4 (or 1); pin the thread; tweet 1
works standalone as the hook. Fill tweet 6's benchmark line from the run
before posting — never post with the placeholder.*
