"""Shared test setup: no test may call OPSIN or PubChem."""
import pytest

from filips_agent import resolve


@pytest.fixture(autouse=True)
def no_structure_lookups(monkeypatch):
    monkeypatch.setattr(resolve, "lookup", lambda source, name, cache_dir=None: [])
