"""Optional domain-specific post-processing, selected by `domain:` in a profile's papers.yaml."""

from __future__ import annotations

import importlib


def load(name: str | None):
    """Return a function enrich(record) -> record. Falls back to a generic review flag."""
    if not name:
        return _generic
    module = importlib.import_module(f"litagent.domains.{name}")
    return module.enrich


def load_paper_flags(name: str | None):
    """Return a function paper_flags(full_text) -> dict of fields added to every record from that paper."""
    if not name:
        return lambda text: {}
    module = importlib.import_module(f"litagent.domains.{name}")
    return getattr(module, "paper_flags", lambda text: {})


def _generic(rec: dict) -> dict:
    reasons = [] if rec.get("evidence_verified") else ["evidence quote not found in source"]
    if rec.get("unit_in_source") is False:
        reasons.append(f"unit '{rec.get('solubility_unit')}' not found in source text")
    rec["needs_review"] = bool(reasons)
    rec["review_reason"] = "; ".join(reasons)
    rec.setdefault("data_caveats", "")
    return rec
