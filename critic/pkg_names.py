"""Curated snapshot of well-known PyPI packages' top-level IMPORT names.

Used by critic/deps.py's typo-suspect check: a name that is one edit away
from something here (but not equal to it and not itself a real import
anywhere) is the "resolvable but wrong" slopsquatting shape — the model
meant `requests` and wrote `requsts`.

This is authored from general knowledge of the Python ecosystem, NOT fetched
live (CodeCouncil is offline by design — no network calls from a static
screening pass). Snapshot date: 2026-07. It deliberately favors precision
over coverage: ~400 solid, unambiguous, commonly-imported names rather than
a padded, half-guessed 1000. Entries are the name actually written after
`import `/`from `, not the PyPI distribution name, so e.g. `scikit-learn`
appears here as `sklearn` and `beautifulsoup4` as `bs4`. All lowercase —
matching against source is case-sensitive exact-lowercase, so packages whose
canonical import name is mixed-case (PIL, Cython, PyQt5, MySQLdb, ...) are
deliberately left out: they'd never match a lowercase-typo'd name anyway,
and a false "known" entry that can't be typed correctly by definition is
worse than no entry.

To extend: add more lowercase, unambiguous top-level import names below,
keeping the list sorted and deduplicated. Do not add ambiguous or generic
words (e.g. single-letter names, very common words) — every entry is a
gravity well that near-miss names get compared against, so noise here
becomes false "typo of X" signals elsewhere.
"""

from __future__ import annotations

PKG_NAMES: tuple[str, ...] = tuple(sorted({
    # --- HTTP / networking ---
    "requests", "urllib3", "httpx", "aiohttp", "certifi", "chardet", "idna",
    "charset_normalizer", "websockets", "websocket", "grpc", "thrift",
    "paramiko", "fabric", "twisted", "gevent", "eventlet", "greenlet",
    "dns", "netifaces", "requests_oauthlib", "oauthlib", "httplib2",
    "h11", "h2", "hpack", "hyperframe", "wsproto", "sniffio", "anyio",
    "trio", "curio", "uvloop", "socketio", "engineio",

    # --- web frameworks / servers ---
    "flask", "django", "fastapi", "starlette", "werkzeug", "jinja2",
    "markupsafe", "itsdangerous", "gunicorn", "uvicorn", "waitress",
    "tornado", "bottle", "pyramid", "sanic", "aiohttp_cors", "channels",
    "graphene", "strawberry", "ariadne", "rest_framework", "corsheaders",
    "flask_sqlalchemy", "flask_login", "flask_wtf", "flask_migrate",
    "flask_cors", "flask_restful", "wtforms",

    # --- data / numeric / scientific ---
    "numpy", "scipy", "pandas", "sklearn", "matplotlib", "seaborn",
    "bokeh", "altair", "holoviews", "plotly", "dash", "xarray", "dask",
    "vaex", "polars", "pyspark", "numba", "sympy", "statsmodels", "patsy",
    "networkx", "h5py", "tables", "zarr", "pyarrow",
    "fastparquet", "joblib", "cloudpickle",

    # --- machine learning / AI ---
    "torch", "torchvision", "torchaudio", "tensorflow", "keras", "jax",
    "flax", "optax", "transformers", "datasets", "tokenizers",
    "accelerate", "diffusers", "sentence_transformers", "huggingface_hub",
    "langchain", "langchain_core", "langchain_community", "langgraph",
    "llama_index", "openai", "anthropic", "cohere", "xgboost", "lightgbm",
    "catboost", "optuna", "hyperopt", "mlflow", "wandb", "tensorboard",
    "gym", "gymnasium", "stable_baselines3", "nltk", "spacy", "gensim",
    "textblob", "onnx", "onnxruntime",

    # --- images / audio / video ---
    "imageio", "skimage", "cv2", "moviepy", "librosa", "soundfile",
    "pyaudio", "sounddevice", "mido", "pydub", "ffmpeg",

    # --- parsing / scraping ---
    "bs4", "lxml", "html5lib", "scrapy", "selenium", "playwright",
    "pyquery", "cssselect", "feedparser", "tldextract",

    # --- serialization / config ---
    "yaml", "toml", "tomli", "tomllib", "orjson", "ujson", "simplejson",
    "msgpack", "avro", "marshmallow", "pydantic",
    "pydantic_settings", "attrs", "attr", "cattrs", "dataclasses_json",
    "typing_extensions", "dotenv", "environs", "dynaconf", "hydra",
    "omegaconf", "configargparse",

    # --- databases / ORMs ---
    "sqlalchemy", "alembic", "psycopg2", "pymysql",
    "pymongo", "redis", "cassandra", "elasticsearch", "opensearchpy",
    "influxdb", "neo4j", "peewee", "tortoise", "asyncpg", "aiomysql",
    "aiosqlite", "clickhouse_driver", "duckdb",

    # --- messaging / task queues ---
    "celery", "kombu", "billiard", "amqp", "pika", "kafka",
    "confluent_kafka", "zmq", "nats", "dramatiq", "rq", "huey",
    "apscheduler", "schedule", "croniter",

    # --- cloud / infra ---
    "boto3", "botocore", "s3transfer", "awscli",
    "docker", "kubernetes", "ansible", "pulumi",

    # --- crypto / security ---
    "cryptography", "jwt", "bcrypt", "passlib", "nacl", "pyotp",
    "cerberus", "argon2", "oscrypto", "certauth",

    # --- cli / terminal ---
    "click", "typer", "rich", "tqdm", "colorama", "termcolor", "blessed",
    "urwid", "textual", "pygments", "docopt", "fire", "questionary",
    "inquirer", "prompt_toolkit", "cleo", "argcomplete",

    # --- packaging / build / dev tooling ---
    "setuptools", "pip", "wheel", "packaging", "pkg_resources",
    "importlib_metadata", "importlib_resources", "zipp", "platformdirs",
    "appdirs", "build", "hatchling", "poetry", "pipenv", "virtualenv",
    "pbr", "versioneer", "semver", "twine", "pip_audit",

    # --- lint / format / type-check ---
    "black", "isort", "flake8", "pylint", "mypy", "pyright", "bandit",
    "safety", "ruff", "autopep8", "yapf", "pre_commit", "commitizen",

    # --- testing ---
    "pytest", "nose", "unittest2", "mock", "hypothesis", "tox", "nox",
    "coverage", "faker", "factory", "behave", "lettuce", "locust",
    "responses", "freezegun", "vcr", "moto", "testfixtures",

    # --- git / vcs / platform apis ---
    "git", "dulwich", "pygit2", "github", "gitlab", "jira", "slack_sdk",
    "slack", "discord", "telegram", "twilio", "sendgrid", "stripe",
    "paypalrestsdk", "braintree", "plaid", "shopify",

    # --- misc utilities ---
    "six", "more_itertools", "toolz", "cytoolz", "cachetools", "wrapt",
    "decorator", "deprecated", "psutil", "watchdog", "humanize", "arrow",
    "pendulum", "dateutil", "pytz", "tzlocal", "send2trash", "pyperclip",
    "keyboard", "pynput", "pyautogui", "pygame", "kivy", "wx", "cffi",
    "pycparser", "pybind11", "swig",
}))
