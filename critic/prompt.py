"""Build the one heartbeat prompt and parse the agent's PASS-or-JSON reply."""

from __future__ import annotations

import json
import re
from typing import Any

MAX_REASONING_EVENTS = 8
MAX_DIFF_CHARS = 12_000

PASS = "PASS"


def heuristics_version(heuristics: str) -> int:
    m = re.search(r"^version:\s*(\d+)", heuristics, re.MULTILINE)
    return int(m.group(1)) if m else 0


def build_prompt(events: list[dict], latest_diff: dict | None, heuristics: str,
                 project: str = "", verdict_history: list[dict] | None = None) -> str:
    reasoning = [e for e in events if e["type"] == "reasoning"][-MAX_REASONING_EVENTS:]
    tools = [e for e in events if e["type"] == "tool_call"]

    parts = []
    if project:
        parts += [project.strip(), ""]
    parts += [f"HEURISTICS (v{heuristics_version(heuristics)}):", heuristics.strip(), ""]

    parts.append("CODING AGENT'S RECENT REASONING:")
    if reasoning:
        parts += [f"- {e['payload']['text']}" for e in reasoning]
    else:
        parts.append("(none this beat)")
    parts.append("")

    parts.append("TOOL CALLS THIS BEAT:")
    if tools:
        for e in tools:
            inp = e["payload"]["input"]
            detail = inp.get("file_path") or inp.get("command") or ""
            parts.append(f"- {e['payload']['tool']} {detail}".rstrip())
    else:
        parts.append("(none)")
    parts.append("")

    parts.append("CURRENT GIT DIFF:")
    if latest_diff and (latest_diff["payload"].get("diff") or latest_diff["payload"].get("untracked")):
        diff = latest_diff["payload"].get("diff", "")
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n… [truncated]"
        parts.append(diff or "(no tracked changes)")
        untracked = latest_diff["payload"].get("untracked", [])
        if untracked:
            parts.append(f"Untracked files: {', '.join(untracked)}")
    else:
        parts.append("(no uncommitted changes)")
    parts.append("")

    contents = (latest_diff or {}).get("payload", {}).get("untracked_contents", {})
    if contents:
        parts.append("NEW FILES (not yet committed):")
        for path, text in sorted(contents.items()):
            parts += [f"--- {path} ---", text.rstrip()]
        parts.append("")

    if verdict_history:
        parts.append("YOUR RECENT VERDICTS ON THIS SESSION:")
        for v in verdict_history:
            loc = f"{v['file']}:{v['line']}" if v.get("line") else v["file"]
            parts.append(f"- [{v['outcome']}] {loc} — {v['issue']}")
        parts.append(
            "Do not re-flag issues already listed above unless new evidence "
            "contradicts the outcome; a rebutted finding is settled."
        )
        parts.append("")

    parts.append(
        "Is there one concrete, high-value issue worth interrupting the developer for? "
        "Respond per your output protocol: exactly PASS, or one raw JSON object."
    )
    return "\n".join(parts)


def parse_reply(raw: str) -> dict[str, Any]:
    """Normalize a reply into a verdict. Malformed output is treated as PASS."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

    if re.fullmatch(r"pass\.?", text, re.IGNORECASE):
        return {"verdict": PASS}

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict) and obj.get("file") and obj.get("issue"):
                return {
                    "verdict": "SUGGESTION",
                    "suggestion": {
                        "file": obj["file"],
                        "line": obj.get("line"),
                        "severity": obj.get("severity", "medium"),
                        "issue": obj["issue"],
                        "rationale": obj.get("rationale", ""),
                    },
                }
        except json.JSONDecodeError:
            pass
    return {"verdict": PASS, "malformed": raw[:500]}
