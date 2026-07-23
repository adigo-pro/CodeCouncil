"""Interactive slash commands for the running council — the Claude Code feel:
CodeCouncil streams in one terminal while you code in another, and `/keys`,
`/model`, `/status` work in place without restarting anything by hand.

Pure command logic lives in `parse_command` (testable, no I/O); side effects
live in `Console`, which the launcher owns. Non-TTY stdin (scripts, CI) never
starts a console — behavior is identical to the pre-console launcher.
"""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path
from typing import Callable

from core import config as cfg
from core.store import read_tail_rows

HELP = """\
commands (while the council runs):
  /keys              set up a model API key (guided, hidden input)
  /model <p/m>       set + persist the primary model (restarts the critic)
  /prober <p/m|off>  set + persist the council prober (restarts the critic)
  /status            daemons, beats, last verdict, heuristics version, keys
  /config            show resolved configuration and where it came from
  /verbose           toggle idle-beat chatter (default: filtered)
  /help              this text
  /quit              stop the council (same as Ctrl-C)"""


def parse_command(line: str) -> tuple[str, str] | None:
    """'/model x' -> ('model', 'x'); non-command lines -> None. Pure."""
    line = line.strip()
    if not line.startswith("/"):
        return None
    parts = line[1:].split(None, 1)
    if not parts:
        return None
    return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


class Console:
    """Owns command side effects. The launcher injects `restart_critic` so the
    console never has to know how subprocesses are managed."""

    def __init__(self, repo: Path, restart_critic: Callable[[], None],
                 stop: Callable[[], None], say: Callable[[str], None]):
        self.repo = repo
        self.restart_critic = restart_critic
        self.stop = stop
        self.say = say

    def handle(self, line: str) -> None:
        parsed = parse_command(line)
        if parsed is None:
            if line.strip():
                self.say("commands start with '/' — try /help")
            return
        cmd, arg = parsed
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            self.say(f"unknown command /{cmd} — try /help")
            return
        try:
            handler(arg)
        except Exception as e:  # a console mishap must never kill the council
            self.say(f"/{cmd} failed: {e}")

    # ---- commands ----
    def _cmd_help(self, _arg: str) -> None:
        self.say(HELP)

    def _cmd_verbose(self, _arg: str) -> None:
        from .signal_filter import VERBOSE
        if VERBOSE.is_set():
            VERBOSE.clear()
            self.say("idle-beat chatter: filtered (default)")
        else:
            VERBOSE.set()
            self.say("idle-beat chatter: shown")

    def _cmd_quit(self, _arg: str) -> None:
        self.stop()

    def _cmd_keys(self, _arg: str) -> None:
        names = list(cfg.KNOWN_KEYS)
        for i, name in enumerate(names, 1):
            self.say(f"  {i}. {name} — {cfg.KNOWN_KEYS[name]}")
        choice = input("which key? [number or name]: ").strip()
        name = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) \
            else choice.upper()
        if not name.endswith("_API_KEY"):
            self.say("expected an *_API_KEY name — aborted")
            return
        value = getpass.getpass(f"{name} (input hidden): ").strip()
        if not value:
            self.say("empty — aborted")
            return
        cfg.update_env_key(name, value)
        self.say(f"{name} saved to {cfg.env_path()} (0600). Takes effect on the "
                 "next model call — no restart needed.")

    def _cmd_model(self, arg: str) -> None:
        if not arg:
            self.say("usage: /model <provider/model>  (e.g. nvidia-nim/nvidia/nemotron-3-super-120b-a12b)")
            return
        cfg.save_config({"model": arg})
        self.say(f"primary model → {arg} (persisted). Restarting the critic…")
        self.restart_critic()

    def _cmd_prober(self, arg: str) -> None:
        if not arg:
            self.say("usage: /prober <provider/model> | /prober off")
            return
        cfg.save_config({"prober": None if arg.lower() == "off" else arg})
        self.say(f"prober → {arg} (persisted). Restarting the critic…")
        self.restart_critic()

    def _cmd_status(self, _arg: str) -> None:
        cc = self.repo / ".codecouncil"
        state = self._json(cc / "state.json")
        cstate = self._json(cc / "critic-state.json")
        heur = (cc / "heuristics.md")
        version = heur.read_text(encoding="utf-8").splitlines()[0] if heur.exists() else "(none)"
        rows = read_tail_rows(cc / "suggestions.ndjsonl")
        last = rows[-1] if rows else None
        from critic.agent import _local_env
        env = _local_env()
        keys = [k for k in cfg.KNOWN_KEYS if env.get(k)]
        self.say(f"observer beat {state.get('beat', '?')} · critic beat {cstate.get('beat', '?')}")
        if last:
            self.say(f"last verdict: {last.get('verdict')} at {last.get('ts', '?')}")
        self.say(f"heuristics: {version}")
        self.say(f"keys available: {', '.join(keys) or 'NONE — run /keys'}")

    def _cmd_config(self, _arg: str) -> None:
        file_cfg = cfg.load_config()
        self.say(f"config file: {cfg.config_path()}")
        self.say(json.dumps(file_cfg or {"(empty)": "flags/env are used"}, indent=2))
        self.say("precedence: CLI flag > env var (COUNCIL_MODEL/COUNCIL_PROBER) > config file")

    @staticmethod
    def _json(p: Path) -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
