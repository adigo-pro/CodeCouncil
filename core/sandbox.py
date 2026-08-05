"""OS-level isolation for code CodeCouncil did not write.

Two loops execute code that is not ours: the Critic runs model-authored
verify/probe scripts (`critic/probe.py`'s `run_script`), and the A/B harness
runs hidden tests that import agent-produced modules (`evals/ab/score.py`).
Both need the same two primitives, and neither loop owns them, so they live
here alongside `core.store`/`core.redact` (the same reasoning that put the
shared knowledge store in `core/knowledge.py`).

WHY AN OS BOUNDARY IS REQUIRED. `run_script` already built a minimal env
allowlist and pointed HOME at the staging dir, and SECURITY.md claimed that
made credentials unreachable. It did not: `HOME` only governs `~` expansion,
so `pwd.getpwuid(os.getuid()).pw_dir` recovers the REAL home regardless, and
reading `<real home>/.codecouncil/env` by absolute path then posting it to
the network was demonstrated end-to-end. Environment scrubbing cannot fix
that -- `getpwuid` reads the OS user database, not the environment -- so the
fix has to deny the syscalls themselves.

WHAT THE PROFILES DO. Deliberately narrow and behavior-preserving: deny all
network egress (the exfiltration channel) and deny reads under the real home
directory (the credential store, `~/.ssh`, shell history), while leaving the
staging directory fully writable so verification and probes still work.

The one subtlety worth stating: **the interpreter itself frequently lives
under the home directory** (pyenv installs to `~/.pyenv`, as does asdf and a
user-local Homebrew), so a profile that blindly denies the whole home breaks
Python before the script ever runs. Both builders therefore re-allow reads of
the interpreter's own prefixes after the home denial -- last matching rule
wins in SBPL, and bwrap applies its binds in order, so the re-allow lands on
top in both.

This is a strong reduction, not a claim of perfect isolation: with
`(allow default)` as the macOS base, a script can still read world-readable
paths outside the home (e.g. another checkout under /opt). Denying the exfil
channel is what makes that materially less useful -- stdout is the only way
back, and callers redact and cap it.
"""

from __future__ import annotations

import os
import pwd
import shutil
import sys
from pathlib import Path

# Policy values for COUNCIL_SANDBOX / the "sandbox" config key.
POLICY_AUTO = "auto"      # sandbox when a mechanism exists, else run without one
POLICY_REQUIRE = "require"  # no mechanism -> refuse to execute at all
POLICY_OFF = "off"        # never sandbox (escape hatch for debugging)
POLICIES = (POLICY_AUTO, POLICY_REQUIRE, POLICY_OFF)


class SandboxUnavailable(RuntimeError):
    """Raised by `wrap` only under POLICY_REQUIRE, when no mechanism exists."""


def resolve_policy(env_value: str | None, config_value: object) -> str:
    """POLICY_AUTO unless env_value (wins) or config_value names another valid
    policy. Unrecognized/missing -> POLICY_AUTO. Pure, mirroring
    hooks.logic.resolve_gate_seconds' precedence shape."""
    raw = env_value if env_value is not None else config_value
    if not isinstance(raw, str):
        return POLICY_AUTO
    value = raw.strip().lower()
    return value if value in POLICIES else POLICY_AUTO


def real_home() -> str:
    """The invoking user's home per the OS user database -- NOT $HOME, which
    callers deliberately point at a staging dir. This is precisely the value a
    hostile script recovers via `pwd.getpwuid`, so it is the value the
    profiles must deny."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser("~")


def interpreter_roots() -> list[str]:
    """Prefixes that must stay readable for Python to start: the interpreter's
    own install and (under a venv) its base install. Returned even when they
    sit under the home directory -- that is the whole point (see module
    docstring: pyenv/asdf put the interpreter in ~)."""
    roots = {sys.base_prefix, sys.prefix}
    exe = os.path.realpath(sys.executable)
    roots.add(str(Path(exe).parent.parent))
    return sorted(r for r in roots if r and r != "/")


def _sbpl_quote(path: str) -> str:
    """Escape a path for an SBPL double-quoted string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def macos_profile(staging: str, home: str, allow_read: list[str]) -> str:
    """SBPL profile for `sandbox-exec -p`. PURE (no I/O) so the rule ordering
    is unit-testable without spawning anything.

    Rule order is load-bearing: SBPL applies the LAST matching rule, so the
    home denial must precede the interpreter/staging re-allows."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        f'(deny file-read* (subpath "{_sbpl_quote(home)}"))',
    ]
    for root in allow_read:
        lines.append(f'(allow file-read* (subpath "{_sbpl_quote(root)}"))')
    lines.append(f'(allow file-read* file-write* (subpath "{_sbpl_quote(staging)}"))')
    return "\n".join(lines)


def bwrap_argv(staging: str, home: str, allow_read: list[str]) -> list[str]:
    """bubblewrap arguments for the same policy on Linux. PURE.

    `--unshare-net` removes network access outright (a stronger guarantee than
    the macOS deny rule). Binds apply in order: the whole filesystem read-only,
    then a tmpfs over the home (hiding credentials), then the interpreter
    prefixes re-bound read-only on top so Python still starts, then staging
    bound writable."""
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", home]
    for root in allow_read:
        argv += ["--ro-bind-try", root, root]
    argv += ["--bind", staging, staging, "--unshare-net", "--die-with-parent"]
    return argv


def mechanism() -> str | None:
    """Which sandbox mechanism this host offers: "sandbox-exec" (macOS),
    "bwrap" (Linux), or None."""
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return None


def wrap(argv: list[str], staging: str, policy: str = POLICY_AUTO,
         home: str | None = None) -> tuple[list[str], bool]:
    """Wrap `argv` so it runs under this host's sandbox.

    Returns `(argv, sandboxed)`. Under POLICY_AUTO an unsandboxable host
    returns the command unchanged with `sandboxed=False` -- callers surface
    that rather than silently implying protection. POLICY_REQUIRE raises
    SandboxUnavailable instead, for operators who would rather lose
    verification than run unsandboxed."""
    if policy == POLICY_OFF:
        return argv, False
    mech = mechanism()
    if mech is None:
        if policy == POLICY_REQUIRE:
            raise SandboxUnavailable(
                "no OS sandbox available (need sandbox-exec on macOS or bwrap on Linux) "
                "and COUNCIL_SANDBOX=require")
        return argv, False
    resolved_home = home if home is not None else real_home()
    roots = interpreter_roots()
    if mech == "sandbox-exec":
        profile = macos_profile(staging, resolved_home, roots)
        return ["/usr/bin/sandbox-exec", "-p", profile, *argv], True
    return [*bwrap_argv(staging, resolved_home, roots), "--", *argv], True


def minimal_env(home: str, pythonpath: str | None = None,
                extra: dict[str, str] | None = None) -> dict[str, str]:
    """A child environment built from scratch -- never `{**os.environ, ...}`.

    No API keys, cloud credentials, or anything else sensitive reaches code we
    did not write. `home` becomes $HOME (callers point it at a throwaway dir so
    `~` expansion resolves somewhere harmless; the OS-level denial above is
    what stops `getpwuid` from routing around that). Empty values are dropped
    so an unset LC_ALL doesn't become an empty override."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
    }
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    if extra:
        env.update(extra)
    return {k: v for k, v in env.items() if v}
