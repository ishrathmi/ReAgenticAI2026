"""Loads the per-user configuration: which papers to process and what to extract.

Shared defaults live in config/papers.yaml and config/schema.json. Each researcher can keep
their own profile in config/<profile>/papers.yaml and config/<profile>/schema.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def load_papers(path: Path = CONFIG_DIR / "papers.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_schema(path: Path = CONFIG_DIR / "schema.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_profile(name: str | None = None) -> tuple:
    """Return (papers_config, schema) for config/<name>/, or the shared defaults if name is None."""
    base = CONFIG_DIR / name if name else CONFIG_DIR
    if not base.is_dir():
        raise FileNotFoundError(f"No profile folder: {base}")
    return load_papers(base / "papers.yaml"), load_schema(base / "schema.json")
