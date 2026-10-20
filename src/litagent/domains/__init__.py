"""Optional domain-specific post-processing, selected by `domain:` in a profile's papers.yaml."""

from __future__ import annotations

import importlib


def load(name: str | None):
    """Return a function enrich(record) -> record. Falls back to a generic review flag."""
    if not name:
        return _generic
    module = importlib.import_module(f"litagent.domains.{name}")
    return module.enrich


def _generic(rec: dict) -> dict:
    rec["needs_review"] = not rec.get("evidence_verified")
    rec["review_reason"] = "" if rec.get("evidence_verified") else "evidence quote not found in source"
    return rec
