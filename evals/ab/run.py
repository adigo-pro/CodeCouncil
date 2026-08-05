"""A/B evaluation: Claude Code WITH CodeCouncil vs WITHOUT vs a naive
self-review nudge, on identical tasks with hidden acceptance tests.

    python3 -m evals.ab.run [--trials N] [--tasks K] [--arms all]

Paired design: every (task, trial) runs once per arm in its own fresh scratch
repo seeded with the same latent-trap files (training.run.SEED_FILES). The
'with' arm gets hooks + observer + critic (the delivery loop — the
treatment), council mode enabled by default via --prober
openrouter/openai/gpt-5-mini (the bake-off's measured high-recall catcher,
4-of-4 vs the precision-anchored primary's 2-of-4 — round 2 saw only 2
findings across 15 with-sessions running the primary alone), and its Stop
hook's done-gate raised to --gate 90s (was 45s; a verified finding was
measured landing ~9s after the old cap already released); 'without' is a
bare repo; 'naive' is also a bare repo (no daemons) but the session's system
prompt gets one generic self-review sentence appended
(NAIVE_REVIEW_PROMPT), via the `claude` CLI's `--append-system-prompt` flag
— the control that isolates whether CodeCouncil's verified review beats the
agent simply nagging itself. Hidden tests (evals.ab.tasks) score the
result; the agent never sees them. Rows land in results.ndjsonl; a markdown
report is printed and written at the end.

--arms accepts a comma list of without|naive|with (e.g. "without,naive,with"),
the alias "all" (-> without,naive,with), or the back-compat alias "both"
(-> without,with, unchanged from before the naive arm existed).

Real-repo substrate (--repo-url URL@sha, opt-in). Default: every trial's
workspace is the synthetic seed (training.run.SEED_FILES) written by
seed_repo — offline, hermetic, no network, unchanged from before this
option existed. Pass --repo-url to instead clone a real, pinned OSS repo
(adapted from ponytail's tiangolo/full-stack-fastapi-template benchmark)
into each FEATURE-tier workspace via clone_repo: `git clone --depth 1`,
then fetch+checkout the pinned sha. This is feature-tier only — safety-tier
trials always seed from their own per-task seed_files (run_safety_trial has
no repo_url parameter at all) because a safety task is a surgical
single-function scenario; a real repo has no bearing on it. A sha is
required (URL@sha) since an unpinned benchmark isn't reproducible as the
upstream repo moves. Using this flag means the shipped TASKS' instructions
and hidden tests (written against the synthetic seed's file layout) no
longer apply as-is — a real run needs tasks/hidden tests written against
that repo's actual structure/tickets.

Contamination-proof arm isolation (credit: the ponytail benchmark project's
most important finding). ponytail nearly published a false ~4% result
because their SessionStart hook fired on EVERY arm, including the baseline
that was supposed to be untreated — the "baseline" was secretly running the
treatment. They caught it by passing `--setting-sources project,local` to
the `claude` CLI, which excludes the user's global/user-level settings so
each arm loads exactly the settings its arm intends. CodeCouncil's without/
naive arms never call install_hooks and spawn no daemons, so they're
isolated at the daemon level — but before this fix, run_session invoked
`claude -p` with no --setting-sources at all, meaning it would inherit
whatever is in the *user's* global `~/.claude/settings.json`. On a
maintainer's machine that has council hooks installed globally (a live
possibility here, since CodeCouncil installs its own hooks into repos it
watches), that would silently contaminate the without/naive arms exactly the
way ponytail's baseline was contaminated. Fix, applied uniformly to every
arm including 'with': every claude invocation in run_session passes
`--setting-sources project,local` (confirmed via `claude --help`, which
lists the accepted values as user, project, local). This excludes 'user'
(global) settings everywhere, so no global hook can ever reach a benchmark
session. The 'with' arm still needs 'project' because that's where
hooks.install writes its repo-local .claude/settings.json — its treatment
is that project-local install plus the observer/critic daemons, never a
global setting, so applying the same flag to it costs nothing and keeps the
rule uniform (one flag, always on, no per-arm branching to get wrong).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from critic import agent  # noqa: E402
from evals.ab import score  # noqa: E402
from evals.ab.safety_tasks import SAFETY_TASKS  # noqa: E402
from evals.ab.tasks import TASKS  # noqa: E402
from hooks.install import install as install_hooks  # noqa: E402
from observer.transcript import find_project_dir  # noqa: E402
from training.run import SEED_FILES, sh  # noqa: E402

SESSION_TIMEOUT = 420
SETTLE_SECONDS = 25  # let the critic judge the session's final diff
# Task 1's done-gate; --gate 0 disables. Raised 45 -> 90 (run-3): a verified
# finding was measured landing ~9s after the old 45s cap already released —
# 90s covers judge+verify with headroom instead of shaving it razor-thin.
GATE_SECONDS_DEFAULT = 90

# run-3: council mode's high-recall prober (docs/benchmarks/ bake-off: 4-of-4
# catches vs the precision-anchored primary's 2-of-4) on the with-arm only —
# round 2 saw just 2 findings across 15 with-sessions running the primary
# alone. '--prober off'/'none' disables (parse_prober below).
PROBER_DEFAULT = "openrouter/openai/gpt-5-mini"

# The naive-control arm's entire "treatment": one generic sentence a user
# might paste themselves, appended via the CLI's --append-system-prompt. Not
# a mini-CodeCouncil — no verification, no repo-specific facts, no tools.
NAIVE_REVIEW_PROMPT = (
    "Before you finish, review your own code for bugs, security issues, and "
    "unhandled edge cases, and fix any you find."
)

ARMS_ALL = ["without", "naive", "with"]
_VALID_ARMS = set(ARMS_ALL)

# Contamination guard (see module docstring: the ponytail lesson). Applied to
# EVERY arm's claude invocation, unconditionally — excludes 'user' (global)
# settings so a council hook in the maintainer's ~/.claude/settings.json can
# never reach a benchmark session. 'project' stays included because the
# 'with' arm's installed hooks live in the repo-local .claude/settings.json.
ISOLATION_SETTING_SOURCES = "project,local"


def parse_repo_url(value: str) -> tuple[str, str]:
    """Parse '--repo-url URL@sha' into (url, sha). A sha is required for
    reproducibility — an unpinned benchmark can silently drift as the
    upstream repo moves (ponytail pins to a commit for exactly this
    reason). Splits on the LAST '@' so ssh-style URLs like
    git@github.com:owner/repo still work once a sha is appended."""
    if "@" not in value:
        raise argparse.ArgumentTypeError(
            f"--repo-url requires a pinned sha for reproducibility: {value!r} "
            "(expected URL@sha, e.g. "
            "https://github.com/tiangolo/full-stack-fastapi-template@<sha>)")
    url, sha = value.rsplit("@", 1)
    if not url or not sha:
        raise argparse.ArgumentTypeError(
            f"--repo-url requires both a URL and a sha: {value!r} (expected URL@sha)")
    return url, sha


class SetupError(RuntimeError):
    """A workspace-setup command (clone/checkout/seed commit) failed — abort
    the trial rather than run a paid session against a garbage workspace."""


def _sh_checked(cmd: list[str], cwd: Path | None = None) -> None:
    """Run a setup command and raise SetupError on nonzero exit. Setup git
    commands were previously unchecked: a failed clone/fetch/checkout still
    proceeded to run_session against an empty or mis-pinned workspace and
    scored the garbage as real data."""
    try:
        r = sh(cmd, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as e:
        raise SetupError(f"{' '.join(cmd)}: {e}") from e
    if r.returncode != 0:
        raise SetupError(f"{' '.join(cmd)} exited {r.returncode}: "
                         f"{(r.stderr or r.stdout).strip()[:200]}")


def clone_repo(repo: Path, url: str, sha: str) -> None:
    """Seed a feature-tier workspace from a real, pinned OSS repo instead of
    the synthetic SEED_FILES — the credible-numbers path (adapted from
    ponytail's tiangolo/full-stack-fastapi-template benchmark). A `--depth 1`
    clone only fetches the tip commit, which usually isn't `sha`, so the
    pinned commit is fetched explicitly before checkout. The real .git is
    kept (not re-init'd): the agent works and commits on top of it, and the
    existing scoring (git_facts, council_stats) already measures the
    session's own commits the normal way."""
    repo.parent.mkdir(parents=True, exist_ok=True)
    _sh_checked(["git", "clone", "--depth", "1", url, str(repo)])
    _sh_checked(["git", "fetch", "--depth", "1", "origin", sha], cwd=repo)
    _sh_checked(["git", "checkout", sha], cwd=repo)
    # verify the pin actually took — a silently-failed checkout would otherwise
    # benchmark the --depth-1 tip, defeating --repo-url's reproducibility.
    head = sh(["git", "rev-parse", "HEAD"], cwd=repo)
    if head.returncode != 0 or head.stdout.strip() != sha:
        raise SetupError(f"checkout of {sha} did not take (HEAD={head.stdout.strip()!r})")


def seed_repo(repo: Path, seed_files: dict[str, str] = SEED_FILES) -> None:
    """Write seed_files into repo and commit. Defaults to the shared
    feature-tier training.run.SEED_FILES; the safety tier passes each task's
    OWN seed_files instead — those are per-task starters, not shared."""
    repo.mkdir(parents=True, exist_ok=True)
    for name, content in seed_files.items():
        dest = repo / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    _sh_checked(["git", "init", "-qb", "main"], cwd=repo)
    _sh_checked(["git", "add", "-A"], cwd=repo)
    # Pin identity + disable signing on the seed commit so it succeeds
    # deterministically — on a CI runner with no global git identity the
    # unchecked commit used to fail silently, and on a machine with commit
    # signing it could hang on pinentry.
    _sh_checked(["git", "-c", "user.email=bench@codecouncil.local",
                 "-c", "user.name=CodeCouncil Bench", "-c", "commit.gpgsign=false",
                 "commit", "-qm", "seed demoapp"], cwd=repo)


def materialize(files: dict[str, str], root: Path) -> None:
    """Write a full file tree (e.g. a SafetyTask's good/bad reference) into
    root — no git, no session, just files on disk for a scorer to run
    against. Shared by selftest_safety() and tests/test_ab_safety.py's
    discrimination test so there's one copy of this logic."""
    for path, content in files.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def selftest_safety() -> tuple[bool, list[str]]:
    """Ponytail's `--selftest` lesson: prove every safety scorer can tell
    good from bad BEFORE spending any API budget on it. For each
    SAFETY_TASKS entry, materializes the task's `good` reference into a
    tempdir and runs its adversarial_test (must report safe==True), then
    does the same for `bad` (must report safe==False) — zero `claude`
    calls, filesystem tempdirs only. Returns (all_discriminate, report
    lines), one line per task, so main() and tests can both use it."""
    all_ok = True
    lines = []
    for task in SAFETY_TASKS:
        with tempfile.TemporaryDirectory() as tmp:
            materialize(task.good, Path(tmp))
            good = score.run_adversarial_test(Path(tmp), task.adversarial_test)
        with tempfile.TemporaryDirectory() as tmp:
            materialize(task.bad, Path(tmp))
            bad = score.run_adversarial_test(Path(tmp), task.adversarial_test)
        good_ok, bad_ok = good["safe"], not bad["safe"]
        if good_ok and bad_ok:
            lines.append(f"{task.name}: DISCRIMINATES")
        else:
            all_ok = False
            problems = []
            if not good_ok:
                problems.append("good=UNSAFE")
            if not bad_ok:
                problems.append("bad=SAFE")
            lines.append(f"{task.name}: BROKEN ({', '.join(problems)})")
    return all_ok, lines


def parse_prober(value: str) -> str | None:
    """--prober MODEL / --prober off (or 'none', case-insensitive) -> None,
    disabling council mode entirely. Mirrors critic/main.py's own --prober
    semantics; the harness owns the eval-profile default (PROBER_DEFAULT),
    critic/main.py's own default stays None (single-model, unchanged)."""
    if value.strip().lower() in ("off", "none"):
        return None
    return value


def prober_key_warning(prober: str | None, env: dict) -> str | None:
    """Pure (no I/O): the with-arm's council-mode prober needs its OWN
    credential — an OPENROUTER_API_KEY distinct from whatever backs the
    primary critic (e.g. an NVIDIA-only setup runs the primary fine while
    every prober call fails silently all run). Mirrors codecouncil/main.py's
    preflight() warning style/wording. Callers pass whichever env dict
    they've already resolved (agent.local_env(), which folds in
    ~/.codecouncil/env) so this stays unit-testable without touching I/O."""
    if not prober or not prober.startswith("openrouter/"):
        return None
    if env.get("OPENROUTER_API_KEY"):
        return None
    return (f"warning: --prober '{prober}' needs OPENROUTER_API_KEY (env or "
            "~/.codecouncil/env) — without it, every with-arm council beat's "
            "prober call will fail (the primary critic still runs).")


def start_council(repo: Path, probes: bool = False,
                  prober: str | None = PROBER_DEFAULT) -> list[subprocess.Popen]:
    install_hooks(repo)
    cc = repo / ".codecouncil"
    cc.mkdir(exist_ok=True)

    def spawn(mod: str, *flags: str) -> subprocess.Popen:
        log = (cc / f"{mod}.live.log").open("a")
        return subprocess.Popen([sys.executable, "-u", "-m", mod, str(repo), *flags],
                                cwd=REPO_ROOT, stdout=log, stderr=log)

    # eval-profile timing: round 1's --interval 10 --turn-spacing 20 left the
    # critic too slow relative to these (short) sessions to ever get a turn
    # in; tightened so the critic actually gets a look before the session ends.
    critic_flags = ["--interval", "5", "--turn-spacing", "10"]
    if probes:
        critic_flags.append("--probes")
    if prober:
        critic_flags += ["--prober", prober]
    return [spawn("observer", "--wait"),
            spawn("critic", *critic_flags)]


def run_session(repo: Path, instruction: str, append: str | None = None,
                 gate_seconds: int = 0) -> dict:
    """gate_seconds > 0 injects COUNCIL_GATE_SECONDS into the claude
    subprocess's own environment (not the harness's os.environ, which stays
    untouched) — the Stop hook (hooks/peer_hook.py, Task 1) reads that var
    from its parent process, which is this claude session, so it must reach
    argv's subprocess env, not just this Python process. gate_seconds<=0
    (the without/naive arms, or --gate 0) leaves env unset entirely (None ->
    sh() inherits the ambient environment exactly as before this parameter
    existed), so those arms' sessions can never see the var."""
    argv = ["claude", "-p", instruction, "--permission-mode", "acceptEdits",
            "--allowedTools", "Edit", "Write", "Bash",
            "--setting-sources", ISOLATION_SETTING_SOURCES]
    if append:
        argv += ["--append-system-prompt", append]
    env = None
    if gate_seconds > 0:
        env = dict(os.environ)
        env["COUNCIL_GATE_SECONDS"] = str(gate_seconds)
    t0 = time.time()
    rc, err = -1, ""
    for attempt in (1, 2):
        try:
            r = sh(argv, cwd=repo, timeout=SESSION_TIMEOUT, env=env)
            rc, err = r.returncode, (r.stderr or r.stdout)[-300:]
        except subprocess.TimeoutExpired:
            # a hang is the MOST common transient failure — treat it as a
            # failed attempt and retry, never let it abort the whole paid run.
            rc, err = -1, f"session timed out after {SESSION_TIMEOUT}s"
        except OSError as e:
            rc, err = -1, f"session failed to launch: {e}"[-300:]
        if rc == 0:
            err = ""
            break
        if attempt < 2:
            time.sleep(30 * attempt)
    return {"rc": rc, "seconds": round(time.time() - t0, 1), "error": err}


def run_trial(base: Path, name: str, category: str, instruction: str,
              hidden: str, arm: str, trial: int, probes: bool = False,
              repo_url: tuple[str, str] | None = None,
              gate: int = GATE_SECONDS_DEFAULT,
              prober: str | None = PROBER_DEFAULT) -> dict:
    """repo_url, when given, is a (url, sha) pair (see parse_repo_url): the
    feature-tier workspace is cloned+pinned from a real OSS repo instead of
    the synthetic SEED_FILES. Unset (the default) is unchanged — synthetic,
    offline, hermetic. This substrate is feature-tier only; run_safety_trial
    has no repo_url parameter at all, because a safety task is a surgical
    single-function scenario against its own seed_files — a real repo has
    no bearing on it (see module docstring / task-5-brief)."""
    repo = base / f"{name}-{arm}-t{trial}"
    if repo_url:
        clone_repo(repo, *repo_url)
    else:
        seed_repo(repo)
    daemons = start_council(repo, probes=probes, prober=prober) if arm == "with" else []
    append = NAIVE_REVIEW_PROMPT if arm == "naive" else None
    gate_seconds = gate if arm == "with" else 0
    if daemons:
        time.sleep(3)
    try:
        session = run_session(repo, instruction, append=append, gate_seconds=gate_seconds)
        if daemons:
            time.sleep(SETTLE_SECONDS)
    finally:
        for p in daemons:
            p.terminate()

    hidden_result = score.run_hidden_test(repo, hidden)
    project_dir = find_project_dir(repo)
    commands = score.bash_commands_from_transcript(project_dir) if project_dir else []
    git = score.git_facts(repo)
    row = {"task": name, "category": category, "arm": arm, "trial": trial,
           "session": session, "hidden": hidden_result,
           "tests_run": score.tests_run(commands), "bash_commands": len(commands),
           "git": git}
    if arm == "with":
        row["council"] = score.council_stats(repo)
    if category == "claim":
        claimed = "tested" in git["last_subject"].lower()
        row["false_claim"] = claimed and not row["tests_run"]
    return row


def run_safety_trial(base: Path, task, arm: str, trial: int, probes: bool = False,
                     gate: int = GATE_SECONDS_DEFAULT,
                     prober: str | None = PROBER_DEFAULT) -> dict:
    """Mirrors run_trial for the SAFETY tier: seeds the task's OWN
    seed_files (not the shared SEED_FILES), runs the arm's session, then
    executes the adversarial_test against the produced repo. The row
    records safe True/False instead of a hidden-test pass-fraction."""
    repo = base / f"{task.name}-{arm}-t{trial}"
    seed_repo(repo, task.seed_files)
    daemons = start_council(repo, probes=probes, prober=prober) if arm == "with" else []
    append = NAIVE_REVIEW_PROMPT if arm == "naive" else None
    gate_seconds = gate if arm == "with" else 0
    if daemons:
        time.sleep(3)
    try:
        session = run_session(repo, task.instruction, append=append, gate_seconds=gate_seconds)
        if daemons:
            time.sleep(SETTLE_SECONDS)
    finally:
        for p in daemons:
            p.terminate()

    adversarial = score.run_adversarial_test(repo, task.adversarial_test)
    project_dir = find_project_dir(repo)
    commands = score.bash_commands_from_transcript(project_dir) if project_dir else []
    git = score.git_facts(repo)
    row = {"task": task.name, "category": "safety", "arm": arm, "trial": trial,
           "session": session, "safe": adversarial["safe"], "adversarial": adversarial,
           "tests_run": score.tests_run(commands), "bash_commands": len(commands),
           "git": git}
    if arm == "with":
        row["council"] = score.council_stats(repo)
    return row


def _council_note(r: dict) -> list[str]:
    notes = []
    if r.get("council"):
        c = r["council"]
        notes.append(f"{c['findings']} finding(s), {c['receipts']} receipt(s), "
                     f"{c.get('delivered', 0)} delivered")
    if r["session"]["rc"] != 0:
        notes.append(f"session rc={r['session']['rc']}")
    return notes


def _error_feature_row(name: str, category: str, arm: str, trial: int, err: str) -> dict:
    """A feature trial that failed to run at all — a crashed hidden result (so
    the crash→0 mean treats it as a 0) plus the error for the report."""
    return {"task": name, "category": category, "arm": arm, "trial": trial,
            "session": {"rc": -1, "seconds": 0.0, "error": err[:300]},
            "hidden": {"passed": 0, "total": 0, "all_pass": False, "checks": {},
                       "crashed": True, "output": err[:300]},
            "tests_run": False, "bash_commands": 0, "git": {},
            "error": err[:300]}


def _error_safety_row(name: str, arm: str, trial: int, err: str) -> dict:
    """A safety trial that failed to run — scored UNSAFE (a trial that couldn't
    even execute earns no SAFE credit)."""
    return {"task": name, "arm": arm, "trial": trial,
            "session": {"rc": -1, "seconds": 0.0, "error": err[:300]},
            "safe": False, "tests_run": False, "error": err[:300]}


def report(rows: list[dict]) -> str:
    feature_rows = [r for r in rows if "hidden" in r]
    safety_rows = [r for r in rows if "safe" in r]
    lines: list[str] = []
    totals: dict[str, list[float]] = {}
    crash_counts: dict[str, int] = {}
    if feature_rows:
        lines += ["| task | arm | hidden tests | tests run | notes |",
                  "|---|---|---|---|---|"]
    for r in feature_rows:
        h = r["hidden"]
        crashed = h.get("crashed") or (not h["total"] and h.get("output"))
        frac = f"{h['passed']}/{h['total']}" if h["total"] else "crash"
        totals.setdefault(r["arm"], [])
        if h["total"]:
            totals[r["arm"]].append(h["passed"] / h["total"])
        elif crashed:
            # A crashed hidden test scores 0, NOT excluded from the mean: a
            # dependency-hallucination crash before any CHECK line prints is
            # exactly the failure mode the benchmark exists to measure (the
            # 'closest-match' task engineers it on purpose). Dropping it let
            # whichever arm crashed more report an inflated pass rate.
            totals[r["arm"]].append(0.0)
            crash_counts[r["arm"]] = crash_counts.get(r["arm"], 0) + 1
        notes = (["FALSE CLAIM"] if r.get("false_claim") else []) + _council_note(r)
        lines.append(f"| {r['task']} | {r['arm']} | {frac} | "
                     f"{'yes' if r['tests_run'] else 'no'} | {'; '.join(notes)} |")
    if safety_rows:
        lines += ["", "| safety task | arm | adversarial | tests run | notes |",
                  "|---|---|---|---|---|"]
    for r in safety_rows:
        lines.append(f"| {r['task']} | {r['arm']} | "
                     f"{'SAFE' if r['safe'] else 'UNSAFE'} | "
                     f"{'yes' if r['tests_run'] else 'no'} | {'; '.join(_council_note(r))} |")
    # stable, readable order for the arms we know about; any unknown arm
    # (custom --arms values) still gets a line, appended after.
    all_arms = set(totals) | {r["arm"] for r in safety_rows}
    order = [a for a in ARMS_ALL if a in all_arms] + \
            [a for a in all_arms if a not in ARMS_ALL]
    safety_totals = score.safe_rate(safety_rows)
    for arm in order:
        if arm in totals:
            vals = totals[arm]
            mean = sum(vals) / len(vals) if vals else 0.0
            crashed = crash_counts.get(arm, 0)
            suffix = f" ({crashed} crashed → scored 0)" if crashed else ""
            lines.append("")
            lines.append(f"**{arm}:** mean hidden-test pass rate "
                         f"{mean:.0%} over {len(vals)} trials{suffix}")
        if arm in safety_totals:
            n_safe, n_total = safety_totals[arm]
            rate = n_safe / n_total if n_total else 0.0
            lines.append("")
            lines.append(f"**{arm}:** safe-rate {n_safe}/{n_total} ({rate:.0%})")
    return "\n".join(lines)


def parse_arms(value: str) -> list[str]:
    """'without,naive,with' -> list; 'all' -> all three; 'both' -> without,with
    (kept as a back-compat alias for the pre-naive-arm --arms both)."""
    if value == "all":
        return list(ARMS_ALL)
    if value == "both":
        return ["without", "with"]
    arms = value.split(",")
    for arm in arms:
        if arm not in _VALID_ARMS:
            raise argparse.ArgumentTypeError(
                f"invalid arm {arm!r} (choose from {sorted(_VALID_ARMS)}, "
                f"'all', or 'both')")
    return arms


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="evals.ab.run", description=__doc__)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--tasks", type=int, default=None,
                    help="first K tasks only (applies to whichever tier(s) run)")
    ap.add_argument("--arms", type=parse_arms, default=parse_arms("both"),
                    help="comma list of without|naive|with, or 'all', or 'both' "
                         "(back-compat for without,with)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--probes", action="store_true",
                    help="enable critic property probes on the with-council arm "
                         "(default off)")
    ap.add_argument("--gate", type=int, default=GATE_SECONDS_DEFAULT,
                    help="done-gate seconds for the with-council arm's Stop hook "
                         "(COUNCIL_GATE_SECONDS) — lets the critic finish judging "
                         "before a short session's 'done' goes through; without/"
                         "naive arms never see this var, gate or no gate. "
                         f"Default {GATE_SECONDS_DEFAULT}s; 0 disables.")
    ap.add_argument("--prober", type=parse_prober, default=PROBER_DEFAULT,
                    help="council mode's second, high-recall model, asked "
                         "alongside the with-arm's precision-anchored primary "
                         "critic (critic/main.py's --prober); verified before "
                         "delivery, never applies to without/naive (no critic "
                         f"spawned there at all). Default {PROBER_DEFAULT!r} "
                         "(the bake-off's measured 4-of-4 catcher); "
                         "'off'/'none' disables council mode. Requires "
                         "OPENROUTER_API_KEY for an openrouter/* model — "
                         "missing it warns at startup rather than crashing.")
    ap.add_argument("--tier", choices=["feature", "safety", "both"], default="feature",
                    help="feature = today's hidden-test TASKS (default); safety = "
                         "SAFETY_TASKS, scored by executing adversarial input; "
                         "both = run both tiers")
    ap.add_argument("--selftest", action="store_true",
                    help="run ONLY the zero-API scorer self-test: prove every "
                         "safety scorer discriminates its good ref (SAFE) from "
                         "its bad ref (UNSAFE); exit nonzero if any doesn't")
    ap.add_argument("--repo-url", type=parse_repo_url, default=None,
                    metavar="URL@SHA",
                    help="clone a real OSS repo pinned to SHA for feature-tier "
                         "workspaces instead of the synthetic seed (credible, "
                         "ponytail-style numbers; adapted from their "
                         "tiangolo/full-stack-fastapi-template benchmark). "
                         "Requires network and a sha (unpinned isn't "
                         "reproducible). Default (unset) is unchanged: synthetic, "
                         "offline, hermetic. Safety-tier trials always use their "
                         "own per-task seed_files and ignore this flag — a real "
                         "repo has no bearing on a surgical single-function "
                         "safety scenario.")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        ok, lines = selftest_safety()
        print("A/B self-test: safety scorers, zero API spend "
              f"({len(SAFETY_TASKS)} task(s))")
        for line in lines:
            print(f"  {line}")
        print("\nALL SCORERS DISCRIMINATE" if ok else
              "\nBROKEN SCORER(S) — fix before spending API budget on a live run")
        return 0 if ok else 1

    tasks = TASKS[:args.tasks] if args.tasks else TASKS
    s_tasks = SAFETY_TASKS[:args.tasks] if args.tasks else SAFETY_TASKS
    arms = args.arms
    run_feature = args.tier in ("feature", "both")
    run_safety = args.tier in ("safety", "both")
    if "with" in arms:
        warn = prober_key_warning(args.prober, agent.local_env())
        if warn:
            print(warn, file=sys.stderr)
    base = (args.out or Path.home() / "tmp" / f"cc-ab-{int(time.time())}").resolve()
    base.mkdir(parents=True, exist_ok=True)
    results = base / "results.ndjsonl"
    n_total = 0
    if run_feature:
        n_total += len(tasks) * len(arms) * args.trials
    if run_safety:
        n_total += len(s_tasks) * len(arms) * args.trials
    print(f"A/B eval: tier={args.tier} × {arms} × {args.trials} trial(s) "
          f"= {n_total} sessions → {base}")
    if args.repo_url:
        url, sha = args.repo_url
        print(f"  feature-tier workspaces: clone of {url} pinned to {sha} "
              "(network required); safety-tier trials ignore --repo-url")

    rows = []
    done = 0
    if run_feature:
        for trial in range(1, args.trials + 1):
            for name, category, instruction, hidden in tasks:
                for arm in arms:
                    done += 1
                    print(f"[{done}/{n_total}] {name} · {arm} · trial {trial} …",
                          flush=True)
                    try:
                        row = run_trial(base, name, category, instruction, hidden,
                                        arm, trial, probes=args.probes,
                                        repo_url=args.repo_url, gate=args.gate,
                                        prober=args.prober)
                    except Exception as e:
                        # one failed setup/trial must not abort a multi-hour paid
                        # run: record it (crashed hidden -> scored 0) and continue.
                        print(f"    TRIAL ERROR: {e}", flush=True)
                        row = _error_feature_row(name, category, arm, trial, str(e))
                    rows.append(row)
                    with results.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    h = row["hidden"]
                    print(f"    hidden {h['passed']}/{h['total']} · "
                          f"tests_run={row['tests_run']} · "
                          f"{row['session']['seconds']}s", flush=True)
    if run_safety:
        for trial in range(1, args.trials + 1):
            for task in s_tasks:
                for arm in arms:
                    done += 1
                    print(f"[{done}/{n_total}] {task.name} · {arm} · trial {trial} …",
                          flush=True)
                    try:
                        row = run_safety_trial(base, task, arm, trial, probes=args.probes,
                                               gate=args.gate, prober=args.prober)
                    except Exception as e:
                        print(f"    TRIAL ERROR: {e}", flush=True)
                        row = _error_safety_row(task.name, arm, trial, str(e))
                    rows.append(row)
                    with results.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    print(f"    safe={row['safe']} · tests_run={row['tests_run']} · "
                          f"{row['session']['seconds']}s", flush=True)

    md = report(rows)
    (base / "report.md").write_text(md + "\n", encoding="utf-8")
    print("\n" + md)
    print(f"\nrows: {results}\nreport: {base / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
