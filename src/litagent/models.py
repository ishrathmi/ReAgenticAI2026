"""Model adapters.

OpenRouterModel calls a real model through OpenRouter (OpenAI-compatible API).
FakeModel returns scripted replies, so tests run offline without an API key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterModel:
    def __init__(self, model: str | None = None):
        from openai import OpenAI

        load_dotenv()
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        self.client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)

    def chat(self, messages: list[dict], **kwargs) -> str:
        response = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
        return response.choices[0].message.content or ""


@dataclass
class FakeModel:
    """Returns the scripted replies in order. Use in tests."""

    replies: list[str] = field(default_factory=list)
    calls: list[list[dict]] = field(default_factory=list)

    def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError("FakeModel has no scripted replies left.")
        return self.replies.pop(0)
