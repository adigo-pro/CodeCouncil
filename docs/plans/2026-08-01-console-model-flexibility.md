# Console Model Flexibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the slash-command console the complete, reliable way to configure CodeCouncil's models — `/keys` + `/model` alone must always produce a working setup, for any supported provider key.

**Architecture:** Four changes: (1) per-key model auto-defaults in `critic/agent.py` so any single key yields a working model; (2) per-knob console-override tracking in `codecouncil/main.py` so `/model`/`/prober` beat a stale `--model` flag or `COUNCIL_MODEL` env var; (3) `/model` validation + informative bare `/model` in `codecouncil/console.py`; (4) `/keys` chaining that offers the matching model after a key is saved. Shared provider/key/default maps and pure helpers live in `core/config.py` (the existing shared config utility — everything else imports from there, keeping the no-cross-loop-imports rule intact).

**Tech Stack:** Python 3.10+ stdlib only. Tests: stdlib `unittest` in `tests/test_codecouncil.py` and `tests/test_agent.py`.

## Global Constraints

- Python is stdlib-only: no pip dependencies anywhere in observer/critic/reflector/hooks/codecouncil/core.
- Tests are stdlib `unittest`; anything touching model calls stubs via `CRITIC_CMD`.
- Console tests must NEVER touch the real `~/.codecouncil` — patch `cfg.CONFIG_DIR` to a tempdir (existing `TestConsoleParsing.setUp` pattern) and patch `critic.agent.LOCAL_ENV_FILE` where `local_env()` is reached.
- A console mishap must never kill the council — all `_cmd_*` exceptions are swallowed by `Console.handle`.
- Precedence rule everywhere: CLI flag > env var > config file > auto-default > pi default. A console command replaces the flag layer and invalidates the env layer *for that knob only*.
- Validation warns, never blocks — `/model` always saves what the user typed (pi may support providers we don't know).
- Lint must pass: `pipx run --spec 'ruff==0.15.22' ruff check .`
- Work on branch `console-model-flexibility` (created in Task 1, Step 0).

---

### Task 1: Shared maps + pure helpers in `core/config.py`

**Files:**
- Modify: `core/config.py` (after `KNOWN_KEYS`, around line 27)
- Test: `tests/test_codecouncil.py` (new class `TestModelHelpers`)

**Interfaces:**
- Produces: `cfg.PROVIDER_KEYS: dict[str, str]` (provider prefix → required key name), `cfg.KEY_DEFAULT_MODELS: tuple[tuple[str, str], ...]` (ordered (key name, default model)), `cfg.check_model(model: str, env: dict[str, str]) -> list[str]` (pure, warning strings), `cfg.resolve_with_source(flag, env_name, config_key, env, base=None) -> tuple[str | None, str]` (value + one of `"flag"`, `"env:<NAME>"`, `"config"`, `"default"`). `cfg.resolve` keeps its exact signature and behavior (delegates).

- [ ] **Step 0: Create the working branch**

```bash
cd /Users/adityagollamudi/code/CodeCouncil && git checkout -b console-model-flexibility
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_codecouncil.py`:

```python
class TestModelHelpers(unittest.TestCase):
    """Task: console-model-flexibility — shared provider/key/default maps."""

    def test_maps_are_consistent(self):
        from core import config as cfg
        # every auto-default's key is a known key, and its provider maps back to it
        for key, model in cfg.KEY_DEFAULT_MODELS:
            self.assertIn(key, cfg.KNOWN_KEYS)
            provider = model.split("/", 1)[0]
            self.assertEqual(cfg.PROVIDER_KEYS.get(provider), key)
        # every known key has an auto-default (so /keys alone always works)
        self.assertEqual({k for k, _ in cfg.KEY_DEFAULT_MODELS}, set(cfg.KNOWN_KEYS))
        # free NVIDIA first, Anthropic last (decorrelation caveat)
        self.assertEqual(cfg.KEY_DEFAULT_MODELS[0][0], "NVIDIA_API_KEY")
        self.assertEqual(cfg.KEY_DEFAULT_MODELS[-1][0], "ANTHROPIC_API_KEY")

    def test_check_model_missing_key(self):
        from core import config as cfg
        warns = cfg.check_model("openrouter/openai/gpt-5-mini", {})
        self.assertTrue(any("OPENROUTER_API_KEY" in w for w in warns))
        self.assertFalse(cfg.check_model("openrouter/openai/gpt-5-mini",
                                         {"OPENROUTER_API_KEY": "sk-or-x"}))

    def test_check_model_shapes(self):
        from core import config as cfg
        env = {"OPENROUTER_API_KEY": "x", "NVIDIA_API_KEY": "x", "OPENAI_API_KEY": "x"}
        # no slash at all
        self.assertTrue(cfg.check_model("gpt-5-mini", env))
        # openrouter and nvidia-nim ids nest — a single segment after the prefix is wrong
        self.assertTrue(any("nested" in w or "full" in w
                            for w in cfg.check_model("openrouter/gpt-5-mini", env)))
        self.assertTrue(any("nested" in w or "full" in w
                            for w in cfg.check_model("nvidia-nim/nemotron-3-super", env)))
        # well-formed values are clean
        self.assertFalse(cfg.check_model("openai/gpt-5-mini", env))
        self.assertFalse(cfg.check_model("nvidia-nim/nvidia/nemotron-3-super-120b-a12b", env))

    def test_check_model_unknown_provider_is_soft_note(self):
        from core import config as cfg
        warns = cfg.check_model("mistral/mistral-large", {})
        self.assertTrue(any("unknown provider" in w for w in warns))

    def test_resolve_with_source(self):
        import tempfile
        from core import config as cfg
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self.assertEqual(cfg.resolve_with_source("f", "E", "k", {"E": "e"}, base),
                             ("f", "flag"))
            self.assertEqual(cfg.resolve_with_source(None, "E", "k", {"E": "e"}, base),
                             ("e", "env:E"))
            cfg.save_config({"k": "c"}, base)
            self.assertEqual(cfg.resolve_with_source(None, "E", "k", {}, base),
                             ("c", "config"))
            self.assertEqual(cfg.resolve_with_source(None, "E", "other", {}, base),
                             (None, "default"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_codecouncil.TestModelHelpers -v`
Expected: FAIL — `AttributeError: module 'core.config' has no attribute 'KEY_DEFAULT_MODELS'` (and friends).

- [ ] **Step 3: Implement in `core/config.py`**

Insert after the `KNOWN_KEYS` dict (line 27):

```python
# provider prefix (first path segment of a "provider/model-id" value) -> the
# API key that provider needs. /model uses this to warn at set time instead
# of letting a missing key surface as per-beat critic failures.
PROVIDER_KEYS = {
    "nvidia-nim": "NVIDIA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}

# When no model is configured anywhere, the first key present picks a default
# so a single /keys entry always yields a working council. Ordered: free
# NVIDIA first; Anthropic last (a critic from the coding agent's own family
# shares its blind spots — README "Model providers" caveat).
KEY_DEFAULT_MODELS = (
    ("NVIDIA_API_KEY", "nvidia-nim/nvidia/nemotron-3-super-120b-a12b"),
    ("OPENROUTER_API_KEY", "openrouter/openai/gpt-5-mini"),
    ("OPENAI_API_KEY", "openai/gpt-5-mini"),
    ("GROQ_API_KEY", "groq/openai/gpt-oss-120b"),
    ("GEMINI_API_KEY", "google/gemini-3-flash-preview"),
    ("ANTHROPIC_API_KEY", "anthropic/claude-haiku-4-5"),
)

# providers whose model ids nest a vendor path (openrouter/openai/gpt-5-mini,
# nvidia-nim/nvidia/nemotron-…) — a single segment after the prefix 404s
_NESTED_ID_PROVIDERS = ("openrouter", "nvidia-nim")


def check_model(model: str, env: dict[str, str]) -> list[str]:
    """Warnings (never errors) for a /model value. Pure — no I/O."""
    if "/" not in model:
        return [f"'{model}' doesn't look like provider/model-id — e.g. openai/gpt-5-mini"]
    provider, rest = model.split("/", 1)
    warns = []
    key = PROVIDER_KEYS.get(provider)
    if key and not env.get(key):
        warns.append(f"{provider}/… needs {key}, which isn't set — run /keys first")
    if key is None:
        warns.append(f"unknown provider '{provider}' — if pi doesn't support it, "
                     "every critic beat will fail (see pi.dev/docs for providers)")
    if provider in _NESTED_ID_PROVIDERS and "/" not in rest:
        warns.append(f"{provider} model ids are nested — expected the full path, "
                     f"e.g. {dict(KEY_DEFAULT_MODELS)[PROVIDER_KEYS[provider]]}")
    return warns
```

Then replace the existing `resolve` function (line 89-92) with:

```python
def resolve_with_source(flag: str | None, env_name: str, config_key: str,
                        env: dict[str, str], base: Path | None = None
                        ) -> tuple[str | None, str]:
    """resolve() plus WHERE the value came from: 'flag' | 'env:<NAME>' |
    'config' | 'default' — so /model and /status can show the layer that won."""
    if flag:
        return flag, "flag"
    if env.get(env_name):
        return env[env_name], f"env:{env_name}"
    v = load_config(base).get(config_key)
    if v:
        return v, "config"
    return None, "default"


def resolve(flag: str | None, env_name: str, config_key: str,
            env: dict[str, str], base: Path | None = None) -> str | None:
    """The one precedence rule: flag > env var > config file > None."""
    return resolve_with_source(flag, env_name, config_key, env, base)[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_codecouncil -v`
Expected: all PASS (new class + existing classes untouched).

- [ ] **Step 5: Commit**

```bash
git add core/config.py tests/test_codecouncil.py
git commit -m "core.config: provider/key maps, /model validation, resolve_with_source"
```

---

### Task 2: Per-key model auto-defaults in `critic/agent.py`

**Files:**
- Modify: `critic/agent.py:41` (constant) and `critic/agent.py:74-81` (`_resolve_model`)
- Test: `tests/test_agent.py` (new class `TestDefaultModelOrder`)

**Interfaces:**
- Consumes: `cfg.KEY_DEFAULT_MODELS` from Task 1.
- Produces: `_resolve_model(model, env)` now falls back through the ordered key list; `DEFAULT_NVIDIA_MODEL` stays defined (existing tests reference it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
class TestDefaultModelOrder(unittest.TestCase):
    """Any single API key must yield a working model with zero /model step."""

    def test_each_key_alone_picks_its_default(self):
        from core import config as cfg
        from critic.agent import _resolve_model
        for key, default in cfg.KEY_DEFAULT_MODELS:
            self.assertEqual(_resolve_model(None, {key: "x"}), default, key)

    def test_nvidia_wins_when_multiple_keys(self):
        from critic.agent import DEFAULT_NVIDIA_MODEL, _resolve_model
        env = {"OPENAI_API_KEY": "x", "NVIDIA_API_KEY": "x", "ANTHROPIC_API_KEY": "x"}
        self.assertEqual(_resolve_model(None, env), DEFAULT_NVIDIA_MODEL)

    def test_explicit_and_env_still_win(self):
        from critic.agent import _resolve_model
        env = {"NVIDIA_API_KEY": "x", "COUNCIL_MODEL": "openai/gpt-5-mini"}
        self.assertEqual(_resolve_model(None, env), "openai/gpt-5-mini")
        self.assertEqual(_resolve_model("groq/openai/gpt-oss-120b", env),
                         "groq/openai/gpt-oss-120b")

    def test_no_keys_resolves_none(self):
        from critic.agent import _resolve_model
        self.assertIsNone(_resolve_model(None, {}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_agent.TestDefaultModelOrder -v`
Expected: FAIL — `test_each_key_alone_picks_its_default` fails for every key except NVIDIA (current code returns None for them).

- [ ] **Step 3: Implement in `critic/agent.py`**

Add the import at the top (after `from pathlib import Path`):

```python
from core import config as cfg
```

Replace `_resolve_model` (lines 74-81) with:

```python
def _resolve_model(model: str | None, env: dict[str, str]) -> str | None:
    """One source of truth for model precedence: explicit param > COUNCIL_MODEL
    env > first configured key's default (cfg.KEY_DEFAULT_MODELS order: free
    NVIDIA first, Anthropic last) > None. The CRITIC_CMD stub branch and the
    pi branch must never drift apart on this — the stub's argv[2] IS the test
    contract for what production would have sent."""
    explicit = model or env.get("COUNCIL_MODEL")
    if explicit:
        return explicit
    for key, default in cfg.KEY_DEFAULT_MODELS:
        if env.get(key):
            return default
    return None
```

Keep `DEFAULT_NVIDIA_MODEL = "nvidia-nim/nvidia/nemotron-3-super-120b-a12b"` at line 41 unchanged (tests and the NVIDIA extension docs reference it). Update the module docstring's COUNCIL_MODEL entry (lines 13-16) to end: `defaults to the first configured key's model (core.config.KEY_DEFAULT_MODELS order), else pi's own default`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_agent -v`
Expected: all PASS, including the pre-existing `test_default_model_only_when_unset_and_key_present`.

- [ ] **Step 5: Commit**

```bash
git add critic/agent.py tests/test_agent.py
git commit -m "critic.agent: any configured API key now auto-selects a default model"
```

---

### Task 3: Per-knob console-override in the launcher + settings_info

**Files:**
- Modify: `codecouncil/main.py:91-97` (`resolve_settings`), `codecouncil/main.py:100-190` (`main`: console_set wiring, `settings_info`, preflight message pass-through), `codecouncil/main.py:43-46` (preflight no-model message mentions `/keys`)
- Test: `tests/test_codecouncil.py` (new class `TestConsoleOverrideResolution`)

**Interfaces:**
- Consumes: `cfg.resolve`, `cfg.resolve_with_source`, `cfg.KEY_DEFAULT_MODELS` (Task 1); `agent.local_env()`.
- Produces: `resolve_settings(args, console_set=frozenset())` — knobs named in `console_set` ignore the CLI flag and env var and read config.json only. `settings_info()` closure (passed to Console in Task 4) returning `{"model", "model_source", "prober", "prober_source", "env"}` where `model_source` is `"flag" | "env:COUNCIL_MODEL" | "config" | "auto:<KEY>" | "default"` and `env` is `agent.local_env()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_codecouncil.py`:

```python
class TestConsoleOverrideResolution(unittest.TestCase):
    """/model must beat a stale --model flag or exported COUNCIL_MODEL: a knob
    in console_set resolves from config.json only."""

    def setUp(self):
        import tempfile
        from core import config as cfg
        self.td = tempfile.TemporaryDirectory()
        self._orig = cfg.CONFIG_DIR
        cfg.CONFIG_DIR = Path(self.td.name)
        self.addCleanup(lambda: setattr(cfg, "CONFIG_DIR", self._orig))
        self.addCleanup(self.td.cleanup)

    @staticmethod
    def _args(model=None, prober=None):
        import argparse
        return argparse.Namespace(model=model, prober=prober)

    def test_flag_and_env_win_normally(self):
        import codecouncil.main as launcher
        with unittest.mock.patch.dict("os.environ", {"COUNCIL_PROBER": "e/p"}, clear=False):
            os.environ.pop("COUNCIL_MODEL", None)
            model, prober = launcher.resolve_settings(self._args(model="f/m"))
            self.assertEqual((model, prober), ("f/m", "e/p"))

    def test_console_set_drops_flag_and_env_for_that_knob_only(self):
        from core import config as cfg
        import codecouncil.main as launcher
        cfg.save_config({"model": "c/m"})
        with unittest.mock.patch.dict(
                "os.environ", {"COUNCIL_MODEL": "e/m", "COUNCIL_PROBER": "e/p"}, clear=False):
            model, prober = launcher.resolve_settings(
                self._args(model="f/m", prober="f/p"), console_set={"model"})
            self.assertEqual(model, "c/m")   # flag + env ignored, config wins
            self.assertEqual(prober, "f/p")  # untouched knob keeps flag precedence

    def test_console_set_prober_off_resolves_none(self):
        import codecouncil.main as launcher
        with unittest.mock.patch.dict("os.environ", {"COUNCIL_PROBER": "e/p"}, clear=False):
            _, prober = launcher.resolve_settings(
                self._args(prober="f/p"), console_set={"prober"})
            self.assertIsNone(prober)  # config has no prober -> council off
```

Also add `import os` and `import unittest.mock` at the top of the file if not present (check existing imports first — the file already imports `unittest`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_codecouncil.TestConsoleOverrideResolution -v`
Expected: FAIL — `TypeError: resolve_settings() got an unexpected keyword argument 'console_set'`.

- [ ] **Step 3: Implement in `codecouncil/main.py`**

Replace `resolve_settings` (lines 91-97) with:

```python
def resolve_settings(args, console_set: frozenset | set = frozenset()
                     ) -> tuple[str | None, str | None]:
    """flag > env var > ~/.codecouncil/config.json — one rule for both knobs.
    A knob named in console_set was just set via /model or /prober: the console
    persisted it to config.json, so the launch-time flag and any exported env
    var must stop outranking it — that knob resolves from the config file only."""
    from core import config as cfg
    env = dict(os.environ)

    def one(knob: str, flag: str | None, env_name: str, key: str) -> str | None:
        if knob in console_set:
            return cfg.resolve(None, env_name, key, {})
        return cfg.resolve(flag, env_name, key, env)

    return (one("model", args.model, "COUNCIL_MODEL", "model"),
            one("prober", args.prober, "COUNCIL_PROBER", "prober"))
```

In `main()`, add after `args = ap.parse_args(argv)` (line 108):

```python
    console_set: set[str] = set()  # knobs reconfigured via /model | /prober
```

Change `launch()`'s first line (line 128) from `m, p = resolve_settings(args)` to:

```python
        m, p = resolve_settings(args, console_set)
```

Add a `settings_info` closure right before the `if sys.stdin.isatty():` block (line 175):

```python
    def settings_info() -> dict:
        """Resolved model/prober + which layer won — for /model and /keys.
        Adds the auto-default layer below config: with no explicit model, the
        critic falls to the first configured key's default (critic/agent.py's
        _resolve_model), and the console should show that truthfully."""
        from core import config as cfg
        env_file = agent.local_env()   # includes ~/.codecouncil/env keys
        env = dict(os.environ)

        def one(knob, flag, env_name, key):
            if knob in console_set:
                return cfg.resolve_with_source(None, env_name, key, {})
            return cfg.resolve_with_source(flag, env_name, key, env)

        m, msrc = one("model", args.model, "COUNCIL_MODEL", "model")
        if m is None:
            for k, d in cfg.KEY_DEFAULT_MODELS:
                if env_file.get(k):
                    m, msrc = d, f"auto:{k}"
                    break
        p, psrc = one("prober", args.prober, "COUNCIL_PROBER", "prober")
        return {"model": m, "model_source": msrc,
                "prober": p, "prober_source": psrc, "env": env_file}
```

Update the Console construction (lines 177-179) to pass the new hooks (their Console-side signatures land in Task 4):

```python
        console = Console(repo=repo, restart_critic=restart_critic,
                          stop=stopping.set,
                          say=lambda m: print(f"{_tag('critic')} {m}", flush=True),
                          settings_info=settings_info,
                          on_override=console_set.add)
```

Finally, in `preflight()` change the no-model warning (lines 44-46) to:

```python
        warns.append("no model configured and no API key found: type /keys in this "
                     "console once the council starts (guided, hidden input), or pass "
                     "--model / set COUNCIL_MODEL / add a key to ~/.codecouncil/env.")
```

Keep the assertion in the existing test `test_warns_when_no_model_and_no_key` in mind — open `tests/test_codecouncil.py:20-24` and check what substring it asserts; if it asserts on `"no model configured"` the new text still matches, otherwise update the assertion to `self.assertIn("/keys", warns[0])`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_codecouncil -v`
Expected: all PASS (new class + `TestPreflight` still green).

- [ ] **Step 5: Commit**

```bash
git add codecouncil/main.py tests/test_codecouncil.py
git commit -m "launcher: /model and /prober now beat stale --model flags and env vars"
```

---

### Task 4: Console — /model validation, informative bare /model, /keys chaining

**Files:**
- Modify: `codecouncil/console.py` (constructor line 47-52, `_cmd_keys` line 86-102, `_cmd_model` line 104-110, `_cmd_prober` line 112-118, HELP line 20-29)
- Test: `tests/test_codecouncil.py` (new class `TestConsoleModelFlow`)

**Interfaces:**
- Consumes: `cfg.check_model`, `cfg.PROVIDER_KEYS`, `cfg.KEY_DEFAULT_MODELS` (Task 1); `settings_info` + `on_override` callables (Task 3 shapes).
- Produces: `Console(repo, restart_critic, stop, say, settings_info=None, on_override=None)` — both new kwargs optional and None-safe, so existing constructions/tests stay valid.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_codecouncil.py`:

```python
class TestConsoleModelFlow(unittest.TestCase):
    """Task: console-model-flexibility — /model validation, bare /model info,
    /keys -> model chaining."""

    def setUp(self):
        import tempfile
        from core import config as cfg
        self.td = tempfile.TemporaryDirectory()
        self._orig = cfg.CONFIG_DIR
        cfg.CONFIG_DIR = Path(self.td.name)
        self.addCleanup(lambda: setattr(cfg, "CONFIG_DIR", self._orig))
        self.addCleanup(self.td.cleanup)
        self.msgs, self.restarts, self.overrides = [], [], []
        self.info = {"model": None, "model_source": "default",
                     "prober": None, "prober_source": "default", "env": {}}

    def _console(self):
        from codecouncil.console import Console
        return Console(repo=Path("."), restart_critic=lambda: self.restarts.append(1),
                       stop=lambda: None, say=self.msgs.append,
                       settings_info=lambda: dict(self.info),
                       on_override=self.overrides.append)

    def test_model_missing_key_warns_but_still_saves(self):
        from core import config as cfg
        self._console().handle("/model openrouter/openai/gpt-5-mini")
        self.assertTrue(any("OPENROUTER_API_KEY" in m for m in self.msgs))
        self.assertEqual(cfg.load_config().get("model"), "openrouter/openai/gpt-5-mini")
        self.assertEqual(self.overrides, ["model"])
        self.assertEqual(self.restarts, [1])

    def test_bare_model_shows_current_source_and_examples(self):
        self.info.update(model="openai/gpt-5-mini", model_source="config",
                         env={"OPENAI_API_KEY": "x"})
        self._console().handle("/model")
        joined = "\n".join(self.msgs)
        self.assertIn("openai/gpt-5-mini", joined)
        self.assertIn("config", joined)
        self.assertNotIn("Restarting", joined)   # bare /model never restarts
        self.assertEqual(self.restarts, [])

    def test_bare_model_with_no_keys_points_at_keys(self):
        self._console().handle("/model")
        self.assertTrue(any("/keys" in m for m in self.msgs))

    def test_prober_gets_same_validation(self):
        self._console().handle("/prober openrouter/openai/gpt-5-mini")
        self.assertTrue(any("OPENROUTER_API_KEY" in m for m in self.msgs))
        self.assertEqual(self.overrides, ["prober"])

    def test_prober_off_skips_validation(self):
        self._console().handle("/prober off")
        self.assertFalse(any("warning" in m for m in self.msgs))

    def test_keys_offers_model_switch_when_current_uses_other_key(self):
        from core import config as cfg
        self.info.update(model="nvidia-nim/nvidia/nemotron-3-super-120b-a12b",
                         model_source="auto:NVIDIA_API_KEY")
        answers = iter(["3", "y"])   # 3 = OPENAI_API_KEY in KNOWN_KEYS order
        with unittest.mock.patch("builtins.input", lambda *_: next(answers)), \
             unittest.mock.patch("getpass.getpass", lambda *_: "sk-test"):
            self._console().handle("/keys")
        self.assertEqual(cfg.load_config().get("model"), "openai/gpt-5-mini")
        self.assertEqual(self.overrides, ["model"])
        self.assertEqual(self.restarts, [1])

    def test_keys_no_switch_when_declined(self):
        from core import config as cfg
        self.info.update(model="nvidia-nim/nvidia/nemotron-3-super-120b-a12b",
                         model_source="config")
        answers = iter(["3", "n"])
        with unittest.mock.patch("builtins.input", lambda *_: next(answers)), \
             unittest.mock.patch("getpass.getpass", lambda *_: "sk-test"):
            self._console().handle("/keys")
        self.assertIsNone(cfg.load_config().get("model"))
        self.assertEqual(self.restarts, [])

    def test_keys_announces_when_key_matches_current_model(self):
        # saving the key the current model already uses -> no prompt at all
        self.info.update(model="openai/gpt-5-mini", model_source="auto:OPENAI_API_KEY")
        with unittest.mock.patch("builtins.input", lambda *_: "3"), \
             unittest.mock.patch("getpass.getpass", lambda *_: "sk-test"):
            self._console().handle("/keys")
        self.assertTrue(any("openai/gpt-5-mini" in m for m in self.msgs))
        self.assertEqual(self.restarts, [])
```

Note: `test_keys_announces_when_key_matches_current_model` patches `input` to always return `"3"` — safe because when no switch prompt happens, `input` is only called once (key choice). `settings_info` is called AFTER the key is saved, so the test's static `self.info` stands in for the post-save resolution.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_codecouncil.TestConsoleModelFlow -v`
Expected: FAIL — `TypeError: Console.__init__() got an unexpected keyword argument 'settings_info'`.

- [ ] **Step 3: Implement in `codecouncil/console.py`**

Constructor (lines 47-52) becomes:

```python
    def __init__(self, repo: Path, restart_critic: Callable[[], None],
                 stop: Callable[[], None], say: Callable[[str], None],
                 settings_info: Callable[[], dict] | None = None,
                 on_override: Callable[[str], None] | None = None):
        self.repo = repo
        self.restart_critic = restart_critic
        self.stop = stop
        self.say = say
        self.settings_info = settings_info
        self.on_override = on_override or (lambda _knob: None)
```

`_cmd_model` (lines 104-110) becomes:

```python
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
```

`_cmd_prober` (lines 112-118) becomes:

```python
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
```

Add the helpers (after `_cmd_prober`):

```python
    def _env(self) -> dict:
        """Key material for validation: settings_info's env when injected
        (launcher passes agent.local_env(), which includes ~/.codecouncil/env),
        else read it directly."""
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
```

In `_cmd_keys`, replace the final `self.say(...)` (lines 101-102) with:

```python
        cfg.update_env_key(name, value)
        self.say(f"{name} saved to {cfg.env_path()} (0600). Takes effect on the "
                 "next model call — no restart needed.")
        self._offer_model_for_key(name)
```

and add:

```python
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
```

Update HELP (lines 20-29): change the `/model` line to
`/model [p/m]       show or set + persist the primary model (set restarts the critic)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_codecouncil -v`
Expected: all PASS, including the untouched `TestConsoleParsing` (old 4-kwarg construction still valid).

- [ ] **Step 5: Commit**

```bash
git add codecouncil/console.py tests/test_codecouncil.py
git commit -m "console: /model validates + shows sources, /keys chains into model choice"
```

---

### Task 5: Docs, full suite, lint

**Files:**
- Modify: `README.md` (Model providers section line ~152-186, console block line ~129-134), `CLAUDE.md` (Model boundary paragraph — the "unless NVIDIA_API_KEY" sentence)
- Test: full suite + ruff

- [ ] **Step 1: Update CLAUDE.md**

In the **Model boundary** paragraph, replace the sentence
`If NVIDIA_API_KEY resolves (real env or that file) and COUNCIL_MODEL is unset, the default becomes NVIDIA-hosted Nemotron (nvidia-nim/nvidia/nemotron-3-super-120b-a12b) — zero pi login required.`
with:
`If COUNCIL_MODEL is unset, the first configured key picks the default model (core.config.KEY_DEFAULT_MODELS, ordered: free NVIDIA-hosted Nemotron first, Anthropic last for decorrelation) — zero pi login required.`

- [ ] **Step 2: Update README.md**

In the console block (~line 129), update the `/model` line to `show or set + persist the primary model`. In "Model providers", after the provider table add one sentence: `With a key configured and no model set, CodeCouncil picks that provider's table entry automatically — /keys alone is a working setup; /model only needed to switch.` Change step 3 of the NVIDIA instructions from the `echo` one-liner to: `Run codecouncil and type /keys — guided, hidden input (the echo one-liner still works for scripts: echo 'NVIDIA_API_KEY=nvapi-...' >> ~/.codecouncil/env).`

- [ ] **Step 3: Full suite + lint**

Run: `python3 -m unittest discover -s tests` — expected: all pass, zero failures.
Run: `pipx run --spec 'ruff==0.15.22' ruff check .` — expected: clean.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: /keys + auto-default model is the headline setup path"
```

---

## Self-Review

- **Spec coverage:** (1) override fix → Task 3; (2) /model validation → Tasks 1+4; (3) informative bare /model → Tasks 3+4 (`settings_info` + `_model_info`); (4) per-key defaults + /keys chaining → Tasks 2+4. Docs → Task 5. ✓
- **Placeholder scan:** all steps carry real code/commands. ✓
- **Type consistency:** `settings_info() -> dict` with keys `model/model_source/prober/prober_source/env` used identically in Tasks 3 and 4; `on_override(knob: str)` matches `console_set.add`; `resolve_settings(args, console_set)` matches test callers; `check_model(model, env) -> list[str]` consistent. ✓
