"""Settings such as API keys: energy_extractor/.env (gitignored) first, then the environment.

See .env.example for the names that are read.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def setting(name, env_file=None):
    """The value of `name` in the .env file, else the environment variable of that name, else None."""
    path = Path(env_file or ENV_FILE)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip("\"'")
    return os.environ.get(name) or None
