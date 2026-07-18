"""Thin wrapper around one non-interactive OpenClaw agent turn in the NemoClaw sandbox.

Multi-line prompts cannot pass through `exec` argv, so the prompt is uploaded
as a file and the in-sandbox agent reads it with $(cat ...).

Set CRITIC_CMD to a local executable to stub the sandbox in tests: it is run
as `$CRITIC_CMD <prompt-file>` and its stdout is taken as the agent's reply.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

INBOX = "/sandbox/workspaces/critic/inbox.txt"
TURN_TIMEOUT = 180


class AgentError(Exception):
    pass


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f"timed out: {' '.join(cmd[:4])}…") from e
    except OSError as e:
        raise AgentError(str(e)) from e


def ask(prompt: str, sandbox: str = "codecouncil", agent: str = "critic",
        session: str | None = None) -> str:
    """Run one agent turn and return its raw reply text."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(prompt)
        prompt_file = f.name
    try:
        override = os.environ.get("CRITIC_CMD")
        if override:
            res = _run([override, prompt_file], timeout=60)
            if res.returncode != 0:
                raise AgentError(f"stub failed: {res.stderr.strip()}")
            return res.stdout.strip()

        up = _run(["nemoclaw", sandbox, "upload", prompt_file, INBOX], timeout=60)
        if up.returncode != 0:
            raise AgentError(f"upload failed: {up.stderr.strip() or up.stdout.strip()}")

        sess = f"--session-id {session} " if session else ""
        turn = f'openclaw agent --agent {agent} {sess}-m "$(cat {INBOX})" 2>/dev/null'
        res = _run(
            ["nemoclaw", sandbox, "exec", "--timeout", str(TURN_TIMEOUT), "--",
             "bash", "-lc", turn],
            timeout=TURN_TIMEOUT + 30,
        )
        if res.returncode != 0:
            raise AgentError(f"agent turn failed: {res.stderr.strip() or res.stdout.strip()}")
        reply = [
            ln for ln in res.stdout.splitlines()
            if ln.strip() and "UNDICI" not in ln and "trace-warnings" not in ln
        ]
        return "\n".join(reply).strip()
    finally:
        Path(prompt_file).unlink(missing_ok=True)
