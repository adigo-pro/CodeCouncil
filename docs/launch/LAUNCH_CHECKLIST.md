# CodeCouncil v0.1.0 Launch Checklist

Ordered so each step is safe to do once the ones above it are done. Commands
assume you're in the repo root and `gh` is authenticated as the repo owner.

---

## Phase 0 — Pre-flight (verify, don't assume)

- [ ] **Tree clean and pushed.** `git status` shows nothing; `git log origin/main..HEAD` is empty.
- [ ] **CI green on the tip commit** — all six jobs (python 3.10, python 3.12, ui, lint, installer, bench-selftest):
      `gh api "repos/adigo-pro/CodeCouncil/actions/runs?per_page=1" --jq '.workflow_runs[0].conclusion'` → `success`
- [ ] **Full suite green locally:** `python3 -m unittest discover -s tests` → `OK` (647 tests).
- [ ] **Read the two launch drafts one more time** so you're happy to post them as-is:
      `docs/launch/show-hn.md` and `docs/launch/x-thread.md`. Confirm every number in them
      still matches reality (they were written honest-first; skim for anything you'd phrase differently).

## Phase 1 — Security (do this BEFORE the repo is public)

- [ ] **Rotate the NVIDIA API key.** The `nvapi-…` key was pasted into a chat earlier in the
      project, so treat it as exposed. At [build.nvidia.com](https://build.nvidia.com): revoke the
      old key, generate a new one, and update your local `~/.codecouncil/env`.
      *(The repo itself is clean — the audit confirmed no key is committed — this is only about the
      one that touched the chat.)*
- [ ] **Final secret sweep** (belt-and-suspenders), from repo root:
      `git grep -nE 'nvapi-[A-Za-z0-9]{10,}|sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}'`
      → only fake test fixtures should match. If anything real appears, stop and remove it
      (and rotate that credential) before continuing.

## Phase 2 — Repo settings (still private, prep the public face)

- [ ] **Set the repo description + topics + website** (shows up the moment it's public):
      ```
      gh repo edit adigo-pro/CodeCouncil \
        --description "An AI peer reviewer for AI coding agents — watches Claude Code, verifies findings with executed repros, delivers them in-session." \
        --homepage "https://codecouncil.vercel.app" \
        --add-topic ai,code-review,claude-code,developer-tools,ai-agents,static-analysis,python
      ```
- [ ] **Enable Discussions** — the Code of Conduct and SUPPORT.md link to it, so it must exist or
      those links 404. Settings → General → Features → check **Discussions**
      (or `gh api -X PATCH repos/adigo-pro/CodeCouncil -f has_discussions=true`).
- [ ] **(Optional) Enable "Private vulnerability reporting"** — Settings → Security → check it, so the
      SECURITY.md advisory link works: `gh api -X PATCH repos/adigo-pro/CodeCouncil -f security_and_analysis='{"secret_scanning":{"status":"enabled"}}'` (secret scanning) and toggle private reporting in the UI.

## Phase 3 — Go public

- [ ] **Flip the repo to public:**
      ```
      gh repo edit adigo-pro/CodeCouncil --visibility public --accept-visibility-change-consequences
      ```
- [ ] **Confirm the Community Standards page is 100%:**
      `https://github.com/adigo-pro/CodeCouncil/community` — README, Code of Conduct, Contributing,
      License, Security, issue + PR templates should all be checked.

## Phase 4 — Tag and release

- [ ] **Tag v0.1.0 and push it:**
      ```
      git tag -a v0.1.0 -m "CodeCouncil v0.1.0" && git push origin v0.1.0
      ```
- [ ] **Create the GitHub Release** from the tag, using the CHANGELOG's 0.1.0 section as the body:
      ```
      gh release create v0.1.0 --title "v0.1.0" --notes-file <(sed -n '/## \[0.1.0\]/,/## \[/p' CHANGELOG.md | sed '$d')
      ```
      *(Or create it in the UI and paste the CHANGELOG entry — verify it renders before publishing.)*

## Phase 5 — Verify the public-facing surface actually works

Now that it's public, these become real (they can't be tested while private —
`raw.githubusercontent.com` 404s for private repos):

- [ ] **The one-line install works from the public URL**, in a throwaway environment:
      ```
      FAKE=$(mktemp -d)/h; mkdir -p "$FAKE"
      HOME="$FAKE" sh -c 'curl -fsSL https://raw.githubusercontent.com/adigo-pro/CodeCouncil/main/install.sh | sh'
      test -x "$FAKE/.local/bin/codecouncil" && echo OK
      rm -rf "$(dirname "$FAKE")"
      ```
- [ ] **README badges render** on the public repo page (CI badge shows passing, license badge shows Apache-2.0).
- [ ] **Site links resolve** — open [codecouncil.vercel.app](https://codecouncil.vercel.app): the GitHub
      button, the docs/compare/benchmarks tabs, and the benchmark-writeup links all load (the GitHub
      links were dead while the repo was private).
- [ ] **A blank issue is blocked** and the templates + Discussions/security contact links show up when
      you click "New issue" (proves `config.yml` took effect).

## Phase 6 — Announce (pick a good time)

- [ ] **Timing:** post on a weekday morning US-Eastern (Tue–Thu tends to be best for Show HN). Avoid
      Friday/weekend. Have ~3 uninterrupted hours after posting to respond.
- [ ] **Show HN** — submit `docs/launch/show-hn.md`. Title field = the first line of that file
      ("Show HN: CodeCouncil – an AI reviewer that caught a security bug in its own code"),
      URL field = the GitHub repo (or the site — repo is the stronger HN choice). Paste the body as
      the first comment.
- [ ] **X/Twitter thread** — post `docs/launch/x-thread.md` as a thread; attach `docs/demo.gif` to
      an early tweet; pin the thread.
- [ ] **(Optional) r/programming, r/MachineLearning, Lobsters** — same honest framing; each community
      has different self-promo norms, so read the rules first.

## Phase 7 — First 72 hours (retention is won here)

- [ ] **Respond fast.** First-issue and first-comment response time is the single strongest signal an
      OSS project sends. Even "known, here's the workaround" beats silence.
- [ ] **Watch the HN thread** — engage with criticism honestly (this project's whole brand is honesty;
      a defensive reply undoes the four-run benchmark story).
- [ ] **Label good-first-issues** as people arrive, so newcomers have an on-ramp.
- [ ] **Keep dogfooding visible** — the acceptance-per-heuristics-version curve and any new self-catch
      are good follow-up content.

## Phase 8 — After the dust settles

- [ ] Triage incoming issues; convert recurring questions into README/SUPPORT entries.
- [ ] Pick the next roadmap item from the audit's deferred list (`.superpowers/audit/`, or the
      progress ledger): full OS sandbox for model-executed scripts, harder multi-file benchmark tasks,
      the readability refactors, or a Cursor/Codex adapter (the biggest audience-multiplier).
- [ ] Consider `FUNDING.yml` if you set up GitHub Sponsors.

---

### The four things only you can do (recap)
1. Rotate the NVIDIA key (Phase 1)
2. Flip the repo public (Phase 3)
3. Tag + release v0.1.0 (Phase 4)
4. Post the announcement drafts (Phase 6)

Everything buildable — code, tests, docs, site, installer, benchmarks, community-health files — is
done, green, and shipped.
