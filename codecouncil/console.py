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
from pathlib import Path
from typing import Callable

from core import config as cfg
from core.store import read_tail_rows

HELP = """\
commands (while the council runs):
  /keys              set up a model API key (guided, hidden input)
  /model [p/m]       show or set + persist the primary model (set restarts the critic)
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
                 stop: Callable[[], None], say: Callable[[str], None],
                 settings_info: Callable[[], dict] | None = None,
                 on_override: Callable[[str], None] | None = None):
        self.repo = repo
        self.restart_critic = restart_critic
        self.stop = stop
        self.say = say
        # settings_info: launcher closure -> {model, model_source, prober,
        # prober_source, env}; on_override(knob): tells the launcher a knob was
        # set here, so config.json outranks the launch flag/env from now on.
        self.settings_info = settings_info
        self.on_override = on_override or (lambda _knob: None)

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
        self._offer_model_for_key(name)

    def _cmd_model(self, arg: str) -> None:
        if not arg:
            self._model_info()
            return
        for w in cfg.check_model(arg, self._env()):
            self.say(f"warning: {w}")
        cfg.save_config({"model": arg})
        self.on_override("model")
        self.say(f"primary model → {arg} (persisted). Restarting the critic…")
        self.restart_critic()

    def _cmd_prober(self, arg: str) -> None:
        if not arg:
            self.say("usage: /prober <provider/model> | /prober off")
            return
        if arg.lower() != "off":
            for w in cfg.check_model(arg, self._env()):
                self.say(f"warning: {w}")
        cfg.save_config({"prober": None if arg.lower() == "off" else arg})
        self.on_override("prober")
        self.say(f"prober → {arg} (persisted). Restarting the critic…")
        self.restart_critic()

    def _env(self) -> dict:
        """Key material for validation: settings_info's env when injected
        (the launcher passes agent.local_env(), which includes
        ~/.codecouncil/env), else read it directly."""
        if self.settings_info:
            return self.settings_info().get("env", {})
        from critic.agent import local_env
        return local_env()

    def _model_info(self) -> None:
        """Bare /model: current resolved model, which layer set it, and
        copy-pasteable examples for the keys actually configured."""
        info = self.settings_info() if self.settings_info else {}
        env = self._env()
        model, src = info.get("model"), info.get("model_source")
        if model:
            self.say(f"model: {model}  (source: {src})")
        else:
            self.say("model: pi default (nothing configured)")
        have = [(k, d) for k, d in cfg.KEY_DEFAULT_MODELS if env.get(k)]
        if have:
            self.say("examples for your configured keys:")
            for k, d in have:
                self.say(f"  /model {d}   ({k} ✓)")
        else:
            self.say("no API keys configured — run /keys first")
        self.say("usage: /model <provider/model-id>")

    def _offer_model_for_key(self, key_name: str) -> None:
        """After saving a key, close the loop on the model: if the resolved
        model already runs on this key, say so; otherwise offer this key's
        default so /keys alone always ends in a working, intentional setup."""
        default = dict(cfg.KEY_DEFAULT_MODELS).get(key_name)
        if not default or not self.settings_info:
            return
        info = self.settings_info()   # post-save: env file already updated
        current = info.get("model")
        if not current:
            return
        provider = current.split("/", 1)[0]
        if cfg.PROVIDER_KEYS.get(provider) == key_name:
            self.say(f"critic model: {current} (source: {info.get('model_source')})")
            return
        ans = input(f"switch primary model to {default}? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            cfg.save_config({"model": default})
            self.on_override("model")
            self.say(f"primary model → {default} (persisted). Restarting the critic…")
            self.restart_critic()
        else:
            self.say(f"keeping {current} — `/model {default}` switches later.")

    def _cmd_status(self, _arg: str) -> None:
        cc = self.repo / ".codecouncil"
        state = self._json(cc / "state.json")
        cstate = self._json(cc / "critic-state.json")
        heur = (cc / "heuristics.md")
        version = heur.read_text(encoding="utf-8").splitlines()[0] if heur.exists() else "(none)"
        rows = read_tail_rows(cc / "suggestions.ndjsonl")
        last = rows[-1] if rows else None
        from critic.agent import local_env
        env = local_env()
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
