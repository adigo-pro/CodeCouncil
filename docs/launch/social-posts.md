# Launch posts — X and LinkedIn (personal voice)

---

## X / Twitter

Anthropic shipped Claude 5 last week and the headline is it checks its own
code now. Which is great — and also kind of the whole problem.

A model reviewing its own work has the exact same blind spots as the model
that wrote it. There's research now showing self-checking makes models more
convincing, not more correct. One study: a model grading itself hit a 94%
pass rate while actual correctness was 20%.

So I built the thing I kept wishing existed — a reviewer that uses a
*different* model, and instead of giving an opinion, actually runs your code
to prove the bug is real. Runs locally. Free.

I'm open-sourcing it because a tool whose whole job is "can you trust this"
shouldn't be a black box. And I'd rather it be useful to everyone than sit in
a private repo being mine.

Not pretending it's perfect either — the benchmarks, including the runs where
it tied, are all in there.

If you've got questions or want to build on it with me, my DMs are open.

github.com/adigo-pro/CodeCouncil

---

## LinkedIn

I've been building this for a while, and today I'm putting it out in the open.

The quick version: Claude 5 (and basically every model now) verifies its own
code before it says it's done. Genuinely useful. But a model checking its own
work is the most correlated reviewer you could pick — it can't catch the
mistakes it was trained to make. There's solid 2026 research on this:
self-verification makes models more convincing, not more correct. One study
had a model grading itself hit a 94% pass rate while real correctness sat
at 20%.

CodeCouncil is my answer to that. It's an independent reviewer for AI coding
agents — a second set of eyes from a *different* model that doesn't just give
an opinion, it runs a repro against your code to actually prove a bug exists.
Local, free, watches your session in real time.

Why open-source it instead of trying to make it a startup? Two reasons. One,
a tool whose entire purpose is trust and verification shouldn't be something
you can't audit — that would be a little hypocritical. Two, this is
infrastructure the whole ecosystem needs, and it'll get built better in the
open, with people adding support for their own agents, than it ever would
locked in my repo.

It's Apache-2.0, plain Python, runs on a free key. And I've tried to be honest
about where it falls short — the benchmarks, ties and all, are published.

If you have questions or want to work on it with me, DM me — I'd genuinely
love that.

github.com/adigo-pro/CodeCouncil
