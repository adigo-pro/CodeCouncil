"""User-level configuration: ~/.codecouncil/config.json + the env key file.

Precedence everywhere: CLI flag > environment variable > config file > default
(mirrors the layering coding agents like Claude Code use, so the two tools
feel the same to configure). The env file holds credentials only; config.json
holds preferences. Both live OUTSIDE every repo so nothing here can ever be
committed by a watched project.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".codecouncil"

# key names the /keys wizard knows how to ask for, with hints
KNOWN_KEYS = {
    "NVIDIA_API_KEY": "free Nemotron via build.nvidia.com (nvapi-...)",
    "OPENROUTER_API_KEY": "any OpenRouter model (sk-or-...)",
    "OPENAI_API_KEY": "OpenAI direct (sk-...)",
    "ANTHROPIC_API_KEY": "Anthropic direct (avoid for the critic: decorrelation)",
    "GEMINI_API_KEY": "Google Gemini via aistudio.google.com",
    "GROQ_API_KEY": "Groq-hosted open models (gsk_...)",
}


def config_path(base: Path | None = None) -> Path:
    return (base or CONFIG_DIR) / "config.json"


def env_path(base: Path | None = None) -> Path:
    return (base or CONFIG_DIR) / "env"


def load_config(base: Path | None = None) -> dict:
    p = config_path(base)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (json.JSONDecodeError, OSError):
            pass  # corrupt config is ignored, not fatal — same as state files
    return {}


def save_config(updates: dict, base: Path | None = None) -> dict:
    """Merge `updates` into config.json (None values delete keys). Atomic."""
    p = config_path(base)
    cfg = load_config(base)
    for k, v in updates.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, p)
    return cfg


def update_env_key(name: str, value: str, base: Path | None = None) -> None:
    """Set KEY=value in the env file, replacing an existing line for the same
    key and preserving everything else (comments included). 0600 the file."""
    p = env_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name and "=" in line and not line.lstrip().startswith("#"):
            out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def resolve(flag: str | None, env_name: str, config_key: str,
            env: dict[str, str], base: Path | None = None) -> str | None:
    """The one precedence rule: flag > env var > config file > None."""
    return flag or env.get(env_name) or load_config(base).get(config_key) or None
