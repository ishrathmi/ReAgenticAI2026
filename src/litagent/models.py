"""Model adapters.

OpenRouterModel calls a real model through OpenRouter (OpenAI-compatible API).
FakeModel returns scripted replies, so tests run offline without an API key.
CachedModel wraps either one with a disk cache and a per-run call limit, to protect a
fixed token allowance: re-running on the same papers costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class BudgetExceeded(RuntimeError):
    pass


class OpenRouterModel:
    def __init__(self, model: str | None = None):
        from openai import OpenAI

        load_dotenv()
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        # Fail a stalled call after 90 s and retry up to 3 times, instead of the client default of 600 s
        self.client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key, timeout=90.0, max_retries=3)
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_usage: dict = {}

    def chat(self, messages: list[dict], **kwargs) -> str:
        from openai import BadRequestError

        kwargs.setdefault("extra_body", {"usage": {"include": True}})   # OpenRouter returns cost per call
        try:
            response = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
        except BadRequestError:
            if "response_format" not in kwargs:
                raise
            kwargs.pop("response_format")       # some models lack JSON mode; the prompt still asks for JSON
            response = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
        self.last_usage = {}
        if response.usage:
            self.tokens_in += response.usage.prompt_tokens or 0
            self.tokens_out += response.usage.completion_tokens or 0
            extra = getattr(response.usage, "model_extra", None) or {}
            self.last_usage = {"prompt_tokens": response.usage.prompt_tokens,
                               "completion_tokens": response.usage.completion_tokens,
                               "cost": extra.get("cost")}
        return response.choices[0].message.content or ""


@dataclass
class FakeModel:
    """Returns the scripted replies in order. Use in tests."""

    replies: list[str] = field(default_factory=list)
    calls: list[list[dict]] = field(default_factory=list)
    model: str = "fake"

    def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError("FakeModel has no scripted replies left.")
        return self.replies.pop(0)


class CachedModel:
    """Disk cache + call budget around any model with a .chat(messages, **kwargs) method."""

    def __init__(self, inner, cache_dir: str = ".cache/llm", max_calls: int = 200, logger=None):
        self.inner = inner
        self.logger = logger            # optional RunLogger: records latency, tokens and cost per call
        self.model = getattr(inner, "model", "unknown")
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.max_calls = max_calls
        self.calls = 0
        self.cache_hits = 0

    def chat(self, messages: list[dict], **kwargs) -> str:
        key = hashlib.sha256(json.dumps([self.model, messages, kwargs], sort_keys=True,
                                        default=str).encode()).hexdigest()
        path = self.cache / f"{key}.json"
        if path.exists():
            self.cache_hits += 1
            if self.logger:
                self.logger.llm_call(0.0, None, cache_hit=True)
            return json.loads(path.read_text())["content"]
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"reached max_calls_per_run={self.max_calls}")
        t = time.perf_counter()
        content = self.inner.chat(messages, **kwargs)
        latency = time.perf_counter() - t
        self.calls += 1
        if self.logger:
            self.logger.llm_call(latency, getattr(self.inner, "last_usage", None), cache_hit=False)
        path.write_text(json.dumps({"model": self.model, "content": content}))
        return content
