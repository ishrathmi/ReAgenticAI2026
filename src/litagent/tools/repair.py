"""Repair step: one targeted retry for records flagged with a specific, checkable problem.

The model never judges its own work. It receives the passage, the record and the reason it was
flagged, and may change only the fields that reason concerns. The same checks as before then run on
the corrected record. The correction is kept only if it removes at least one problem and adds none;
otherwise the original record is kept. Every attempt is recorded in repair_* columns.
"""

from __future__ import annotations

import json
import re

from .extract import coerce, unit_in_source, verify_evidence

PROMPT = """You check one record that was extracted from a scientific text. An automatic check flagged it.

Fix ONLY the problems listed. You may change only these fields: {fields}.
Use null when the text does not state a value; never guess or infer.
"repair_evidence" must be a verbatim quote from the PASSAGE or PAPER CONTEXT that supports your answer, or null.

Return ONLY a JSON object: {{"record": {{...the fields you were allowed to change...}}, "repair_evidence": "...", "explanation": "one sentence"}}

PROBLEMS:
{problems}

RECORD:
{record}"""

USER = """PASSAGE:
\"\"\"
{passage}
\"\"\"

PAPER CONTEXT (other sentences from the same paper):
\"\"\"
{context}
\"\"\""""

# Generic repair, available for every profile. Domain modules add their own in REPAIRS.
GENERIC = {
    "evidence quote not found in source": (
        ["evidence"],
        'The evidence quote is not in the passage. Copy the exact span from the PASSAGE that contains the value.'),
}


def repairable(rec: dict, rules: dict) -> list:
    """(reason, fields, instruction) for each flagged reason that has a repair rule."""
    out = []
    for reason in filter(None, str(rec.get("review_reason") or "").split("; ")):
        for key, (fields, instruction) in rules.items():
            if key in reason:
                out.append((reason, fields, instruction))
    return out


def repair(rec: dict, passage: str, paper_text: str, context: str, schema: dict, model, enrich,
           rules: dict, evidence_key: str = "evidence", max_attempts: int = 1) -> dict:
    """Return the record, corrected if a correction passes the checks. Raises BudgetExceeded upward."""
    todo = repairable(rec, rules)
    if not todo:
        return rec
    allowed = sorted({f for _, fields, _ in todo for f in fields})
    before = _reasons(rec)
    # Problems found outside enrich() (schema, table coverage) must survive re-checking
    external = before - _reasons(enrich(dict(rec)))
    fixable = {reason for reason, _, _ in todo}
    current = {k: rec.get(k) for k in schema.get("properties", {}) if not schema["properties"][k].get("x-provenance")}
    notes = []
    for attempt in range(1, max_attempts + 1):
        messages = [
            {"role": "system", "content": PROMPT.format(
                fields=", ".join(allowed),
                problems="\n".join(f"- {reason}: {instruction}" for reason, _, instruction in todo),
                record=json.dumps(current, ensure_ascii=False, default=str))},
            {"role": "user", "content": USER.format(passage=passage, context=context or "(none)")},
        ]
        raw = model.chat(messages, temperature=0, max_tokens=800, response_format={"type": "json_object"})
        reply = _reply(raw)
        proposed = coerce({**current, **{k: v for k, v in (reply.get("record") or {}).items() if k in allowed}}, schema)
        changes = {k: (current.get(k), proposed.get(k)) for k in allowed if proposed.get(k) != current.get(k)}
        quote = reply.get("repair_evidence")
        quote_ok = not quote or verify_evidence(quote, passage + "\n" + paper_text)[0]
        if not changes:
            notes.append(f"attempt {attempt}: no change proposed ({reply.get('explanation') or 'no explanation'})")
            break
        # A new non-null value (other than a new evidence quote, which is verified below) needs a verified quote
        needs_quote = any(v is not None and k != evidence_key for k, (_, v) in changes.items())
        if not quote_ok or (needs_quote and not quote):
            notes.append(f"attempt {attempt}: rejected, repair_evidence " + ("not found in the paper" if quote else "missing"))
            continue
        cand = {**rec, **{k: v for k, (_, v) in changes.items()}}
        if evidence_key in changes:
            ok, ratio = verify_evidence(cand.get(evidence_key), passage)
            cand.update(evidence_verified=ok, evidence_match=ratio)
        if "solubility_unit" in changes:
            cand["unit_in_source"] = unit_in_source(cand.get("solubility_unit"), passage)
        cand = enrich(cand)
        after = _reasons(cand) | external
        cand.update(needs_review=bool(after), review_reason="; ".join(sorted(after)))
        added, removed = after - before, before - after
        if removed & fixable and not added:
            cand.update(repair_attempted=True, repair_outcome="accepted",
                        repaired_fields="; ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in changes.items()),
                        repair_note="; ".join(notes + [f"attempt {attempt}: {reply.get('explanation') or ''}".strip()]))
            return cand
        notes.append(f"attempt {attempt}: rejected, checks " +
                     (f"added: {'; '.join(sorted(added))}" if added else "still fail"))
    rec.update(repair_attempted=True, repair_outcome="rejected" if any("rejected" in n for n in notes) else "no change",
               repaired_fields="", repair_note="; ".join(notes))
    return rec


def _reasons(rec: dict) -> set:
    return set(filter(None, str(rec.get("review_reason") or "").split("; ")))


def _reply(raw: str) -> dict:
    s = re.sub(r"^```(?:json)?|```$", "", str(raw or "").strip(), flags=re.M).strip()
    for text in (s, (re.search(r"\{.*\}", s, re.S) or [None])[0]):
        if not text:
            continue
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}
