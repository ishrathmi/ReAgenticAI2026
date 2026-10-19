"""Loads the per-user configuration: which papers to process and what to extract."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_papers(path: Path = CONFIG_DIR / "papers.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_schema(path: Path = CONFIG_DIR / "schema.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
