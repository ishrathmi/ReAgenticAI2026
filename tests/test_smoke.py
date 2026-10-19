"""Offline checks that the scaffold is wired correctly. No API key needed."""

from litagent.config import load_papers, load_schema
from litagent.models import FakeModel


def test_config_loads():
    assert "dois" in load_papers()
    assert load_schema()["type"] == "object"


def test_fake_model_returns_scripted_reply():
    model = FakeModel(replies=["hello"])
    assert model.chat([{"role": "user", "content": "hi"}]) == "hello"
