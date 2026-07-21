"""Build the one heartbeat prompt and parse the agent's PASS-or-JSON reply."""

from __future__ import annotations

import json
import re
from typing import Any

MAX_REASONING_EVENTS = 8
MAX_TOOL_EVENTS = 15
MAX_DIFF_CHARS = 12_000
PROMPT_BUDGET_CHARS = 16_000
MIN_DIFF_CHARS = 1_000

PASS = "PASS"


def heuristics_version(heuristics: str) -> int:
    m = re.search(r"^version:\s*(\d+)", heuristics, re.MULTILINE)
    return int(m.group(1)) if m else 0


def build_prompt(events: list[dict], latest_diff: dict | None, heuristics: str,
                 project: str = "", verdict_history: list[dict] | None = None) -> str:
    all_reasoning = [e for e in events if e["type"] == "reasoning"]
    all_tools = [e for e in events if e["type"] == "tool_call"]
    reasoning = all_reasoning[-MAX_REASONING_EVENTS:]
    tools = all_tools[-MAX_TOOL_EVENTS:]
    omitted = (len(all_reasoning) - len(reasoning)) + (len(all_tools) - len(tools))

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
    if omitted:
        parts.append(f"(+{omitted} earlier events this batch omitted)")
    parts.append("")

    # diff gets whatever budget the other sections left, floor MIN_DIFF_CHARS
    diff_budget = min(MAX_DIFF_CHARS,
                      max(MIN_DIFF_CHARS, PROMPT_BUDGET_CHARS - sum(len(p) + 1 for p in parts)))
    parts.append("CURRENT GIT DIFF:")
    if latest_diff and (latest_diff["payload"].get("diff") or latest_diff["payload"].get("untracked")):
        diff = latest_diff["payload"].get("diff", "")
        if len(diff) > diff_budget:
            diff = diff[:diff_budget] + "\n… [truncated]"
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

    commits = [e for e in events if e["type"] == "commit"]
    if commits:
        parts.append("JUST COMMITTED:")
        commit_budget = max(2_000, (PROMPT_BUDGET_CHARS - sum(len(p) + 1 for p in parts)) // 2)
        for c in commits:
            parts += [f"- {s}" for s in c["payload"].get("subjects", [])]
            cdiff = c["payload"].get("diff", "")
            if len(cdiff) > commit_budget:
                cdiff = cdiff[:commit_budget] + "\n… [truncated]"
            parts.append(cdiff)
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
        "Is there one concrete, high-value issue worth interrupting the developer for — "
        "either introduced by these changes, OR a serious latent problem visible in the "
        "touched code (a secret, a docstring or comment that promises what the code "
        "doesn't do, an unportable path)? Respond per your output protocol: PASS "
        "(optionally 'PASS: <reason under 15 words>'), or one raw JSON object."
    )
    return "\n".join(parts)


TEST_CMD_RE = re.compile(
    r"\b(pytest|python3? -m unittest|npm (?:run )?test|npx vitest|vitest|jest|go test|cargo test|bun test)\b"
)


def tests_run(events: list[dict]) -> str | None:
    """The test command actually executed in this window, if any — mechanical fact."""
    for e in reversed(events):
        if e.get("type") != "tool_call":
            continue
        cmd = (e.get("payload", {}).get("input") or {}).get("command", "")
        m = TEST_CMD_RE.search(str(cmd))
        if m:
            return m.group(0)
    return None


def build_task_review(events: list[dict], latest_diff: dict | None, heuristics: str,
                      project: str = "", tests_run_sticky: str | None = None) -> str:
    """The 'agent says it's done' review: claims vs what the diff supports.

    tests_run_sticky: the most recent test-command timestamp seen anywhere
    the critic has been watching (bounded to a staleness window), even if
    outside this review's event window or from a different session — see
    critic/main.py's tests_run_at state. Without it, a test run just outside
    the window reads as "no tests were run".
    """
    claims = [e for e in events if e["type"] == "reasoning"][-12:]
    commits = [e for e in events if e["type"] == "commit"]
    ran = tests_run(events)

    parts = []
    if project:
        parts += [project.strip(), ""]
    parts += [f"HEURISTICS (v{heuristics_version(heuristics)}):", heuristics.strip(), ""]
    parts.append("TASK REVIEW — the coding agent has just declared this work finished.")
    parts.append("")
    parts.append("THE AGENT'S STATEMENTS DURING THE TASK (its claims live here):")
    parts += [f"- {e['payload']['text']}" for e in claims] or ["(none captured)"]
    parts.append("")
    if ran:
        fact = f"tests run in this window ({ran})"
    elif tests_run_sticky:
        fact = (f"no test command in this window, but one ran at "
               f"{tests_run_sticky} earlier (possibly another session)")
    else:
        fact = "NO test command was executed"
    parts.append(f"MECHANICAL FACT — {fact}")
    parts.append("")
    if commits:
        parts.append("COMMITS THIS TASK:")
        for c in commits:
            parts += [f"- {s}" for s in c["payload"].get("subjects", [])]
            parts.append(c["payload"].get("diff", "")[:6_000])
        parts.append("")
    diff_budget = min(MAX_DIFF_CHARS,
                      max(MIN_DIFF_CHARS, PROMPT_BUDGET_CHARS - sum(len(p) + 1 for p in parts)))
    parts.append("FINAL UNCOMMITTED DIFF:")
    if latest_diff and latest_diff["payload"].get("diff"):
        parts.append(latest_diff["payload"]["diff"][:diff_budget])
    else:
        parts.append("(clean)")
    parts.append("")
    parts.append(
        "Identify the agent's completion claims. Is any important claim UNSUPPORTED "
        "by the diffs above — something stated as done, handled, or tested that the "
        "code does not show? Judge claims against code, not against style. You are "
        "seeing a WINDOW of the session: a claim about work possibly done before "
        "this window (not visible above) is out of scope — never flag absence of "
        "earlier work, only contradictions with what IS shown. Respond per your "
        "output protocol: PASS (optionally with reason), or one raw JSON object "
        "flagging the most important unsupported claim."
    )
    return "\n".join(parts)


def parse_reply(raw: str) -> dict[str, Any]:
    """Normalize a reply into a verdict. Malformed output is treated as PASS."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

    m = re.fullmatch(r"pass\.?(?:\s*[:—–-]\s*(?P<reason>\S.{0,200}))?", text,
                     re.IGNORECASE | re.DOTALL)
    if m:
        reason = (m.group("reason") or "").strip().rstrip(".")
        return {"verdict": PASS, **({"reason": reason} if reason else {})}

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
