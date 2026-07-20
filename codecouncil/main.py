"""CodeCouncil, one command: installs the hooks, then runs the observer,
critic, and reflector together with prefixed output.

    python3 -m codecouncil /path/to/repo [--model provider/id]

Each loop stays its own process (crash isolation, same code paths as running
them individually); this is only a launcher and output multiplexer.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from hooks.install import install as install_hooks

LOOPS = ["observer", "critic", "reflector"]
COLORS = {"observer": "36", "critic": "33", "reflector": "35"}
_TTY = sys.stdout.isatty()


def _tag(name: str) -> str:
    label = f"[{name:9}]"
    return f"\033[{COLORS[name]}m{label}\033[0m" if _TTY else label


def _pump(name: str, proc: subprocess.Popen) -> None:
    for line in proc.stdout:  # type: ignore[union-attr]
        print(f"{_tag(name)} {line.rstrip()}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="codecouncil", description=__doc__)
    ap.add_argument("repo", type=Path, help="path to the repo being coded in")
    ap.add_argument("--model", help="model for pi turns (sets COUNCIL_MODEL, e.g. openai/gpt-4o)")
    ap.add_argument("--no-hooks", action="store_true", help="skip hook installation")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    if not args.no_hooks:
        added = install_hooks(repo)
        print(f"{_tag('observer')} hooks {'installed: ' + ', '.join(added) if added else 'already installed'}")

    env = os.environ.copy()
    if args.model:
        env["COUNCIL_MODEL"] = args.model

    root = Path(__file__).resolve().parent.parent
    extra = {"observer": ["--wait"], "critic": [], "reflector": []}
    procs: dict[str, subprocess.Popen] = {}
    for name in LOOPS:
        procs[name] = subprocess.Popen(
            [sys.executable, "-u", "-m", name, str(repo), *extra[name]],
            cwd=root, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        threading.Thread(target=_pump, args=(name, procs[name]), daemon=True).start()

    print(f"{_tag('critic')} council running on {repo} — Ctrl-C to stop")
    try:
        # if any loop dies, surface it; keep the others running
        while procs:
            time.sleep(1)
            for name, p in list(procs.items()):
                if p.poll() is not None:
                    print(f"{_tag(name)} exited with code {p.returncode}", flush=True)
                    del procs[name]
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
