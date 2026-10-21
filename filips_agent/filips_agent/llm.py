"""The one place that calls Claude: JSON in a fixed schema, validated, with usage logged.

Every agent goes through ask_json(), so model choice, refusal handling and cost
accounting live here. Usage of every call is appended to data/logs/usage.jsonl.
"""
import base64
import json
import time
from pathlib import Path

import anthropic
import jsonschema

from .ledger import DATA_DIR

MODELS = {
    "inbox": "claude-haiku-5-5",
    "triage": "claude-opus-5-5",   # until the triage trial decides
    "extract": "claude-opus-5-5",
}

# $ per million tokens: input, output, cache read (cache writes cost 1.25x input)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
# server-side refusal fallback ("default" routing) is offered for these models
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

USAGE_LOG = DATA_DIR / "logs" / "usage.jsonl"
# filips_agent/.env (gitignored) holds this project's own key: ANTHROPIC_API_KEY=sk-ant-...
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_client = None


class LLMError(RuntimeError):
    """The model refused, ran out of tokens, or returned JSON that fails the schema."""


def _project_key():
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "ANTHROPIC_API_KEY" and value.strip():
            return value.strip().strip("\"'")
    return None


def client():
    """The project key from .env if there is one, else the ANTHROPIC_API_KEY environment variable."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=_project_key())
    return _client


def pdf_block(path, cache=False):
    """A PDF as a document content block; Claude reads its text, tables and figures."""
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data},
             "title": Path(path).name}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def cost(model, usage):
    p_in, p_out, p_read = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (usage["input_tokens"] * p_in + usage["cache_creation_input_tokens"] * p_in * 1.25
            + usage["cache_read_input_tokens"] * p_read + usage["output_tokens"] * p_out) / 1e6


def ask_json(model, system, content, schema, effort="medium", max_tokens=32000, task="", log=USAGE_LOG):
    """Send one request and return (data, usage) where data validates against schema.

    system: str (cached, so a fixed rules prompt is paid for once per 5 minutes).
    content: list of user content blocks (text, document).
    """
    params = dict(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
    )
    if model in _FALLBACK_MODELS:
        params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    with client().beta.messages.stream(**params) as stream:
        msg = stream.get_final_message()

    u = msg.usage
    usage = {
        "input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
        "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
        "cache_read_input_tokens": u.cache_read_input_tokens or 0,
    }
    usage["cost_usd"] = round(cost(msg.model, usage), 6)
    _log(log, {"time": time.time(), "task": task, "model": msg.model, "requested_model": model,
               "stop_reason": msg.stop_reason, "message_id": msg.id, **usage})

    if msg.stop_reason == "refusal":
        category = msg.stop_details.category if msg.stop_details else None
        raise LLMError(f"{task}: model declined (category: {category})")
    if msg.stop_reason == "max_tokens":
        raise LLMError(f"{task}: output hit max_tokens={max_tokens}")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if text is None:
        raise LLMError(f"{task}: no text in response")
    try:
        data = json.loads(text)
        jsonschema.validate(data, schema)
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        raise LLMError(f"{task}: response does not match the schema ({e})") from e
    return data, usage


def count_tokens(model, system, content):
    """Input tokens a request would use, without running it."""
    return client().messages.count_tokens(
        model=model, system=system, messages=[{"role": "user", "content": content}]).input_tokens


def total_cost(since=0.0, log=USAGE_LOG):
    """Sum of logged cost_usd for calls after `since` (a time.time() value)."""
    if not Path(log).exists():
        return 0.0
    with open(log, encoding="utf-8") as f:
        return sum(r["cost_usd"] for r in map(json.loads, f) if r["time"] >= since)


def _log(path, record):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
