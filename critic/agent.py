"""One non-interactive turn of the pi coding agent (https://pi.dev). Stdlib only.

Judgment turns run with no tools (pure text verdicts); verification turns get
read/bash so the agent can actually run a repro in a staging directory. Every
turn is ephemeral (--no-session) and stripped of pi's discovery machinery, so
the persona passed via --system-prompt is the whole identity.

Env (read from the real environment first, then ~/.codecouncil/env — a local,
never-committed credentials file outside any watched repo):
  PI_BIN          pi executable (default "pi")
  COUNCIL_MODEL   "provider/model" override (e.g. "openai/gpt-4o", or
                  "nvidia-nim/nvidia/nemotron-3-super-120b-a12b" via the
                  bundled NVIDIA provider extension); defaults to pi's own default model,
                  unless NVIDIA_API_KEY is available (then Nemotron 3 Super)
  NVIDIA_API_KEY  enables the bundled NVIDIA-hosted Nemotron provider
  CRITIC_CMD      test stub: run as `$CRITIC_CMD <prompt-file> <resolved-model-or-empty>`,
                  stdout = reply (existing single-arg stubs ignore the second argv)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

TURN_TIMEOUT = 180

CRITIC_PERSONA = Path(__file__).parent / "persona.md"
NVIDIA_EXTENSION = Path(__file__).parent / "pi_extensions" / "nvidia_provider.mjs"
# repo_read/repo_grep/repo_find/repo_ls: path-jailed read-only tools for
# judgment turns (Task 4). Always attached (harmless when tools unused) —
# pi's *builtin* read/grep/find/ls resolve absolute/~ paths outside cwd, so
# they cannot safely be offered to a turn that also has cwd=<watched repo>;
# these jail every path to the repo root and exclude .git/.codecouncil. See
# critic/pi_extensions/repo_tools.mjs for the jail implementation.
REPO_TOOLS_EXTENSION = Path(__file__).parent / "pi_extensions" / "repo_tools.mjs"
LOCAL_ENV_FILE = Path.home() / ".codecouncil" / "env"
DEFAULT_NVIDIA_MODEL = "nvidia-nim/nvidia/nemotron-3-super-120b-a12b"


class AgentError(Exception):
    pass


def local_env() -> dict[str, str]:
    """Real process env, topped up from ~/.codecouncil/env for anything unset.
    That file never lives inside a git repo, so a credential pasted there can
    never be committed by CodeCouncil regardless of which repo is watched."""
    env = dict(os.environ)
    if LOCAL_ENV_FILE.is_file():
        for line in LOCAL_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    return env


def _run(cmd: list[str], timeout: int, cwd: str | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, env=env)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"timed out: {' '.join(cmd[:2])}…") from e
    except OSError as e:
        raise AgentError(str(e)) from e


def _resolve_model(model: str | None, env: dict[str, str]) -> str | None:
    """One source of truth for model precedence: explicit param > COUNCIL_MODEL
    env > NVIDIA default (when its key resolves) > None. The CRITIC_CMD stub
    branch and the pi branch must never drift apart on this — the stub's
    argv[2] IS the test contract for what production would have sent."""
    return model or env.get("COUNCIL_MODEL") or (
        DEFAULT_NVIDIA_MODEL if env.get("NVIDIA_API_KEY") else None
    )


def ask(prompt: str, system: str | None = None, tools: str | None = None,
        cwd: str | None = None, model: str | None = None) -> str:
    """Run one agent turn and return its raw reply text.

    tools: comma-separated pi tool allowlist (e.g. "read,bash"); None = no tools.
    cwd: working directory for the turn — where tool-enabled turns may run code.
    model: explicit "provider/model" override, taking precedence over the
        COUNCIL_MODEL/NVIDIA-default resolution below. This is how a worker
        thread (e.g. council mode's judge_batch, running off the main thread)
        selects a model per call without mutating os.environ, which would
        race the main thread. None = existing resolution unchanged.
    """
    override = os.environ.get("CRITIC_CMD")
    if override:
        env = local_env()
        resolved_model = _resolve_model(model, env) or ""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name
        try:
            # Second argv is the resolved model (may be ""), so multi-model
            # tests can stub per-model replies; existing single-arg stubs
            # (`#!/bin/sh\necho ...`) ignore extra argv — zero breakage.
            res = _run([override, prompt_file, resolved_model], timeout=60)
            if res.returncode != 0:
                raise AgentError(f"stub failed: {res.stderr.strip()}")
            return res.stdout.strip()
        finally:
            Path(prompt_file).unlink(missing_ok=True)

    env = local_env()
    cmd = [env.get("PI_BIN", "pi"), "-p", "--no-session",
           "--no-context-files", "--no-extensions", "--no-skills",
           "--no-prompt-templates", "--no-themes"]
    # -ne above disables discovery of *installed* extensions; explicit -e
    # paths still load (register the "nvidia" provider / repo_* tools),
    # harmless if unused by a given turn.
    if NVIDIA_EXTENSION.is_file():
        cmd += ["--extension", str(NVIDIA_EXTENSION)]
    if REPO_TOOLS_EXTENSION.is_file():
        cmd += ["--extension", str(REPO_TOOLS_EXTENSION)]
    cmd += ["--tools", tools] if tools else ["--no-tools"]
    if system:
        cmd += ["--system-prompt", system]
    resolved_model = _resolve_model(model, env)
    if resolved_model:
        cmd += ["--model", resolved_model]
    cmd.append(prompt)

    res = _run(cmd, timeout=TURN_TIMEOUT, cwd=cwd, env=env)
    if res.returncode != 0:
        raise AgentError(f"pi turn failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()
