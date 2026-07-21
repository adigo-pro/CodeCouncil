"""Find and tail Claude Code session transcripts for a given repo.

Claude Code writes one JSONL file per session to ~/.claude/projects/<munged-path>/.
Each line is a JSON object; `type: "assistant"` lines carry the model's reasoning
and tool calls in message.content blocks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.redact import redact

from .events import DIFF, REASONING, TOOL_CALL, Event  # noqa: F401  (re-export convenience)

REASONING_MAX_CHARS = 1500
COMMAND_MAX_CHARS = 300

PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def munge_path(repo: Path) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", str(repo))


def _dir_matches_cwd(project_dir: Path, repo: Path) -> bool:
    """Check whether ANY transcript in the dir points at `repo` via its cwd field
    (dirs can hold sessions with mixed cwds — one mismatch must not veto the rest)."""
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            with jsonl.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if cwd:
                        if Path(cwd) == repo:
                            return True
                        break  # this session belongs elsewhere; try the next file
        except OSError:
            continue
    return False


def find_project_dir(repo: Path, projects_root: Path = PROJECTS_ROOT) -> Path | None:
    """Locate the transcript dir for a repo: munged-name guess first, then cwd scan."""
    candidate = projects_root / munge_path(repo)
    if candidate.is_dir() and _dir_matches_cwd(candidate, repo):
        return candidate
    if not projects_root.is_dir():
        return None
    for d in projects_root.iterdir():
        if d.is_dir() and _dir_matches_cwd(d, repo):
            return d
    return None


def tail_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Read complete lines past `offset`; never consume a partial trailing line."""
    size = path.stat().st_size
    if size < offset:
        offset = 0  # file was truncated/rotated: re-read
    if size == offset:
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    complete = chunk[: last_nl + 1]
    lines = complete.decode("utf-8", errors="replace").splitlines()
    return lines, offset + last_nl + 1


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [{len(text)} chars total]"


def _salient_tool_input(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    """Compress a tool_use input to what the Critic needs to know."""
    out: dict[str, Any] = {}
    if "file_path" in inp:
        out["file_path"] = inp["file_path"]
    if "command" in inp:
        out["command"] = _truncate(redact(str(inp["command"])), COMMAND_MAX_CHARS)
    if name == "Edit":
        out["old_len"] = len(inp.get("old_string", ""))
        out["new_len"] = len(inp.get("new_string", ""))
    elif name == "Write":
        out["content_len"] = len(inp.get("content", ""))
    return out


def parse_line(raw: str, beat: int) -> list[Event]:
    """Turn one transcript JSONL line into zero or more observation events."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if obj.get("type") != "assistant" or obj.get("isSidechain"):
        return []
    session = obj.get("sessionId")
    events: list[Event] = []
    for block in (obj.get("message") or {}).get("content", []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("thinking", "text"):
            text = (block.get("thinking") if btype == "thinking" else block.get("text")) or ""
            if text.strip():
                events.append(
                    Event(
                        beat=beat,
                        type=REASONING,
                        session=session,
                        payload={"kind": btype,
                                "text": _truncate(redact(text.strip()), REASONING_MAX_CHARS)},
                    )
                )
        elif btype == "tool_use":
            name = block.get("name", "?")
            events.append(
                Event(
                    beat=beat,
                    type=TOOL_CALL,
                    session=session,
                    payload={"tool": name, "input": _salient_tool_input(name, block.get("input") or {})},
                )
            )
    return events


def collect(project_dir: Path, offsets: dict[str, int], beat: int) -> list[Event]:
    """Tail every session file in the project dir; mutates `offsets` in place."""
    events: list[Event] = []
    for jsonl in sorted(project_dir.glob("*.jsonl")):
        key = str(jsonl)
        lines, new_offset = tail_new_lines(jsonl, offsets.get(key, 0))
        offsets[key] = new_offset
        for line in lines:
            events.extend(parse_line(line, beat))
    return events
