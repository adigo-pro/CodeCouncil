"""CodeCouncil, one command: installs the hooks, then runs the observer,
critic, and reflector together with prefixed output.

    python3 -m codecouncil /path/to/repo [--model provider/id]

Each loop stays its own process (crash isolation, same code paths as running
them individually); this is only a launcher and output multiplexer.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from critic import agent
from hooks.install import install as install_hooks

from .signal_filter import DROP, HIGHLIGHT, VERBOSE, classify

_dropped = {"n": 0}  # idle-beat lines filtered so far (see _pump)

KEY_VARS = ("NVIDIA_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY")


def preflight(model: str | None, prober: str | None = None) -> list[str]:
    """Warnings to surface before launching — a misconfigured backend otherwise
    just makes all three loops fail every beat with no obvious cause."""
    warns = []
    pi_bin = os.environ.get("PI_BIN", "pi")
    if shutil.which(pi_bin) is None:
        warns.append(f"'{pi_bin}' not found on PATH — install pi (https://pi.dev) "
                     "or set PI_BIN. The critic and reflector cannot run without it.")
    env = agent.local_env()  # includes ~/.codecouncil/env
    has_key = any(env.get(v) for v in KEY_VARS)
    if not model and not env.get("COUNCIL_MODEL") and not has_key:
        warns.append("no model configured and no API key found: type /keys in this "
                     "console once the council starts (guided, hidden input), or pass "
                     "--model / set COUNCIL_MODEL / add a key to ~/.codecouncil/env. "
                     "pi will fall back to its own default, which may not be authenticated.")
    # Council mode (Task 4): the prober is a second, independent model call
    # (critic/main.py's resolve_prober precedence: --prober flag > this same
    # COUNCIL_PROBER env fallback > None). openrouter/* providers need
    # OPENROUTER_API_KEY specifically — the generic has_key check above can't
    # cover it, since e.g. an NVIDIA key configures the primary just fine
    # while leaving every prober call failing.
    resolved_prober = prober or env.get("COUNCIL_PROBER")
    if resolved_prober and resolved_prober.startswith("openrouter/") and not env.get("OPENROUTER_API_KEY"):
        warns.append(f"prober '{resolved_prober}' needs OPENROUTER_API_KEY: set it "
                     "or put it in ~/.codecouncil/env. Without it, every council beat's "
                     "prober call will fail (the primary verdict still runs).")
    return warns

LOOPS = ["observer", "critic", "reflector"]
COLORS = {"observer": "36", "critic": "33", "reflector": "35", "ui": "32"}
_TTY = sys.stdout.isatty()


def _tag(name: str) -> str:
    label = f"[{name:9}]"
    return f"\033[{COLORS[name]}m{label}\033[0m" if _TTY else label


def _pump(name: str, proc: subprocess.Popen) -> None:
    for line in proc.stdout:  # type: ignore[union-attr]
        text = line.rstrip()
        if name == "ui":
            # the dashboard dev server is chatty; surface only its URL
            if "http://localhost" in text:
                print(f"{_tag('ui')} dashboard ready → {text.split()[-1]}", flush=True)
            continue
        kind = classify(text)
        if kind == DROP and not VERBOSE.is_set():
            _dropped["n"] += 1
            if _dropped["n"] % 30 == 0:
                print(f"{_tag(name)} \033[2m· quiet ({_dropped['n']} idle beats filtered — /verbose to show)\033[0m",
                      flush=True)
            continue
        if kind == HIGHLIGHT:
            print(f"{_tag(name)} \033[1m★ {text}\033[0m", flush=True)
        else:
            print(f"{_tag(name)} {text}", flush=True)


def resolve_settings(args, console_set: frozenset | set = frozenset()
                     ) -> tuple[str | None, str | None]:
    """flag > env var > ~/.codecouncil/config.json — one rule for both knobs.
    A knob named in console_set was just set via /model or /prober: the console
    persisted it to config.json, so the launch-time flag and any exported env
    var must stop outranking it — that knob resolves from the config file only."""
    from core import config as cfg
    from critic import agent
    # local_env(), not os.environ: the critic resolves from ~/.codecouncil/env
    # too (agent.local_env), so the launcher must consult the same layers or a
    # model/prober set only in that file resolves differently here than in the
    # critic it launches.
    env = agent.local_env()

    def one(knob: str, flag: str | None, env_name: str, key: str) -> str | None:
        if knob in console_set:
            return cfg.resolve(None, env_name, key, {})
        return cfg.resolve(flag, env_name, key, env)

    return (one("model", args.model, "COUNCIL_MODEL", "model"),
            one("prober", args.prober, "COUNCIL_PROBER", "prober"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codecouncil", description=__doc__)
    ap.add_argument("repo", type=Path, nargs="?", default=Path("."),
                    help="path to the repo being coded in (default: current directory)")
    ap.add_argument("--model", help="model for pi turns (sets COUNCIL_MODEL, e.g. openai/gpt-4o)")
    ap.add_argument("--prober", help="council mode: second model asked alongside --model/"
                    "COUNCIL_MODEL (e.g. openrouter/openai/gpt-5-mini), or set COUNCIL_PROBER")
    ap.add_argument("--no-hooks", action="store_true", help="skip hook installation")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    console_set: set[str] = set()  # knobs reconfigured via /model | /prober

    model, prober = resolve_settings(args)
    for w in preflight(model, prober):
        print(f"{_tag('critic')} warning: {w}", flush=True)

    if not args.no_hooks:
        added = install_hooks(repo)
        print(f"{_tag('observer')} hooks {'installed: ' + ', '.join(added) if added else 'already installed'}")

    root = Path(__file__).resolve().parent.parent
    procs: dict[str, subprocess.Popen] = {}

    def launch(name: str) -> None:
        # settings re-resolve on every (re)launch so /model and /prober apply
        m, p = resolve_settings(args, console_set)
        env = os.environ.copy()
        if m:
            env["COUNCIL_MODEL"] = m
        if not p:
            # /prober off must win over an exported COUNCIL_PROBER or one in
            # ~/.codecouncil/env: set it empty so the critic's
            # local_env().setdefault can't re-add the file value (resolve_prober
            # treats "" as off). When p is set it goes on the --prober flag
            # below, which outranks env anyway.
            env["COUNCIL_PROBER"] = ""
        extra = {"observer": ["--wait"],
                 "critic": ["--prober", p] if p else [],
                 "reflector": []}[name]
        procs[name] = subprocess.Popen(
            [sys.executable, "-u", "-m", name, str(repo), *extra],
            cwd=root, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        threading.Thread(target=_pump, args=(name, procs[name]), daemon=True).start()

    for name in LOOPS:
        launch(name)

    # dashboard: auto-start when it has been built once; otherwise say how
    ui_dir = root / "ui"
    if (ui_dir / "node_modules").is_dir():
        ui_env = os.environ.copy()
        ui_env["COUNCIL_REPO"] = str(repo)
        procs["ui"] = subprocess.Popen(
            ["npm", "run", "dev"], cwd=ui_dir, env=ui_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        threading.Thread(target=_pump, args=("ui", procs["ui"]), daemon=True).start()
    else:
        print(f"{_tag('ui')} dashboard not built — one-time: cd {ui_dir} && npm install "
              "(then relaunch for http://localhost:4700)", flush=True)

    print(f"{_tag('critic')} model: {model or 'pi default'}"
          + (f" · prober: {prober}" if prober else " · council mode off"), flush=True)

    stopping = threading.Event()

    def restart_critic() -> None:
        old = procs.get("critic")
        if old and old.poll() is None:
            old.send_signal(signal.SIGINT)
            try:
                old.wait(timeout=5)
            except subprocess.TimeoutExpired:
                old.kill()
        launch("critic")

    def settings_info() -> dict:
        """Resolved model/prober + which layer won — for /model and /keys.
        Adds the auto-default layer below config: with no explicit model, the
        critic falls to the first configured key's default (critic/agent.py's
        _resolve_model), and the console should show that truthfully."""
        from core import config as cfg
        env_file = agent.local_env()   # includes ~/.codecouncil/env keys

        def one(knob, flag, env_name, key):
            if knob in console_set:
                return cfg.resolve_with_source(None, env_name, key, {})
            # resolve against local_env (incl. ~/.codecouncil/env), matching the
            # critic — else a COUNCIL_MODEL in that file is missed here and the
            # display wrongly falls through to the auto:<KEY> default.
            return cfg.resolve_with_source(flag, env_name, key, env_file)

        m, msrc = one("model", args.model, "COUNCIL_MODEL", "model")
        if m is None:
            for k, d in cfg.KEY_DEFAULT_MODELS:
                if env_file.get(k):
                    m, msrc = d, f"auto:{k}"
                    break
        p, psrc = one("prober", args.prober, "COUNCIL_PROBER", "prober")
        return {"model": m, "model_source": msrc,
                "prober": p, "prober_source": psrc, "env": env_file}

    console_note = ""
    if sys.stdin.isatty():
        from .console import Console
        console = Console(repo=repo, restart_critic=restart_critic,
                          stop=stopping.set,
                          say=lambda m: print(f"{_tag('critic')} {m}", flush=True),
                          settings_info=settings_info,
                          on_override=console_set.add)

        def _read_stdin() -> None:
            for line in sys.stdin:
                console.handle(line)
                if stopping.is_set():
                    break

        threading.Thread(target=_read_stdin, daemon=True).start()
        console_note = " · type /help for commands"

    print(f"{_tag('critic')} council running on {repo} — Ctrl-C to stop{console_note}")
    try:
        # if any loop dies, surface it; keep the others running
        while procs and not stopping.is_set():
            time.sleep(1)
            for name, p in list(procs.items()):
                if p.poll() is not None:
                    print(f"{_tag(name)} exited with code {p.returncode}", flush=True)
                    del procs[name]
        if stopping.is_set():
            raise KeyboardInterrupt
        return 1
    except KeyboardInterrupt:
        for p in procs.values():
            p.send_signal(signal.SIGINT)
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("\ncouncil stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
