"""Ask the model to extract records matching config/<profile>/schema.json from one chunk of text.

The prompt is built from the schema, so changing what is extracted means editing the
schema and the profile's `task`/`instructions`, not this file.
"""

from __future__ import annotations

import difflib
import json
import re

PROMPT = """{task}

Return ONLY a JSON object of the form {{"records": [ ... ]}}. Return {{"records": []}} if the text contains no relevant data.

Each record has these keys. Use null when the text does not state a value; never guess or infer.
{fields}

Rules:
- "{evidence_key}" must be the shortest verbatim span from the text that contains the reported value. Copy it character for character.
- One record per distinct measurement.
{instructions}"""

USER = """Document: {title}
Location: {label} ({kind})

TEXT:
\"\"\"
{text}
\"\"\""""

NUMERIC = {"number", "integer"}


def _types(spec: dict) -> set:
    t = spec.get("type", "string")
    return set(t if isinstance(t, list) else [t])


def build_system_prompt(schema: dict, task: str, instructions: str = "", evidence_key: str = "evidence") -> str:
    lines = []
    for name, spec in schema.get("properties", {}).items():
        if spec.get("x-provenance"):
            continue                              # filled by the agent, not the model
        t = "/".join(sorted(_types(spec) - {"null"})) or "string"
        desc = spec.get("description", "").rstrip(".")
        options = [str(e) for e in spec.get("enum", []) if e is not None]
        enum = f". One of: {', '.join(options)}, or null" if options else ""
        lines.append(f"- {name} ({t}): {desc}{enum}".rstrip())
    return PROMPT.format(task=task.strip(), fields="\n".join(lines), evidence_key=evidence_key,
                         instructions=instructions.strip())


def parse_json(content: str) -> list:
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    data = None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None
    if data is None:
        data = _salvage(content)          # truncated reply: keep the complete records
    if isinstance(data, dict):
        data = data.get("records", [])
    return [r for r in (data or []) if isinstance(r, dict)]


def _salvage(content: str) -> list:
    """Recover complete {...} records from a reply cut off mid-way (e.g. max_tokens reached)."""
    start = content.find("[")
    if start < 0:
        return []
    out, depth, begin, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(content[start + 1:], start=start + 1):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and begin is not None:
                try:
                    out.append(json.loads(content[begin:i + 1]))
                except json.JSONDecodeError:
                    pass
    return out


def coerce(record: dict, schema: dict) -> dict:
    """Keep only schema fields; turn numeric strings like '7.4' or '25 °C' into numbers."""
    out = {}
    for name, spec in schema.get("properties", {}).items():
        if spec.get("x-provenance"):
            continue
        v = record.get(name)
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a", "na", "not stated"):
            v = None
        if isinstance(v, str) and name.endswith("operator"):
            v = v.replace("≥", ">=").replace("≤", "<=").replace("≈", "~").strip()
        if v is not None and _types(spec) & NUMERIC and isinstance(v, str):
            m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", v.replace("−", "-"))
            v = float(m.group(0)) if m else v
        out[name] = v
    return out


def _squash(s: str) -> str:
    s = s.replace("−", "-").replace("–", "-").replace("μ", "µ").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_evidence(quote, source: str, threshold: float = 0.85) -> tuple:
    """True if the quote appears in the source text (exactly, or as a close fuzzy match)."""
    if not quote:
        return False, 0.0
    q, s = _squash(str(quote)), _squash(source)
    if q in s:
        return True, 1.0
    n, best = len(q), 0.0
    for i in range(0, max(1, len(s) - n + 1), max(1, n // 4)):
        best = max(best, difflib.SequenceMatcher(None, q, s[i:i + n]).ratio())
        if best > 0.95:
            break
    return best >= threshold, round(best, 3)


def extract(text: str, schema: dict, model, system_prompt: str, title: str = "",
            label: str = "", kind: str = "text", evidence_key: str = "evidence",
            max_tokens: int = 4000) -> list:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": USER.format(title=title, label=label, kind=kind, text=text)},
    ]
    raw = model.chat(messages, temperature=0, max_tokens=max_tokens, response_format={"type": "json_object"})
    records = []
    for r in parse_json(raw):
        rec = coerce(r, schema)
        ok, ratio = verify_evidence(rec.get(evidence_key), text)
        rec["evidence_verified"] = ok
        rec["evidence_match"] = ratio
        records.append(rec)
    return records
