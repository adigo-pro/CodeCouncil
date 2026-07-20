"""One non-interactive turn of the pi coding agent (https://pi.dev). Stdlib only.

Judgment turns run with no tools (pure text verdicts); verification turns get
read/bash so the agent can actually run a repro in a staging directory. Every
turn is ephemeral (--no-session) and stripped of pi's discovery machinery, so
the persona passed via --system-prompt is the whole identity.

Env:
  PI_BIN         pi executable (default "pi")
  COUNCIL_MODEL  optional "provider/model" override (e.g. "openai/gpt-4o", or a
                 custom OpenAI-compatible provider configured in pi's settings);
                 otherwise pi's own default model is used
  CRITIC_CMD     test stub: run as `$CRITIC_CMD <prompt-file>`, stdout = reply
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

TURN_TIMEOUT = 180

CRITIC_PERSONA = Path(__file__).parent / "persona.md"


class AgentError(Exception):
    pass


def _run(cmd: list[str], timeout: int, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"timed out: {' '.join(cmd[:2])}…") from e
    except OSError as e:
        raise AgentError(str(e)) from e


def ask(prompt: str, system: str | None = None, tools: str | None = None,
        cwd: str | None = None) -> str:
    """Run one agent turn and return its raw reply text.

    tools: comma-separated pi tool allowlist (e.g. "read,bash"); None = no tools.
    cwd: working directory for the turn — where tool-enabled turns may run code.
    """
    override = os.environ.get("CRITIC_CMD")
    if override:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name
        try:
            res = _run([override, prompt_file], timeout=60)
            if res.returncode != 0:
                raise AgentError(f"stub failed: {res.stderr.strip()}")
            return res.stdout.strip()
        finally:
            Path(prompt_file).unlink(missing_ok=True)

    cmd = [os.environ.get("PI_BIN", "pi"), "-p", "--no-session",
           "--no-context-files", "--no-extensions", "--no-skills",
           "--no-prompt-templates", "--no-themes"]
    cmd += ["--tools", tools] if tools else ["--no-tools"]
    if system:
        cmd += ["--system-prompt", system]
    model = os.environ.get("COUNCIL_MODEL")
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    res = _run(cmd, timeout=TURN_TIMEOUT, cwd=cwd)
    if res.returncode != 0:
        raise AgentError(f"pi turn failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()
