"""Abstract triage (RULES.md A1-A4): does a paper go on the reading list?

Claude answers three yes/no questions; the verdict itself (A4) is applied in code.
"""
from .. import fetch, llm
from ..ledger import IRRELEVANT, TO_READ, TO_TRIAGE
from . import rules_section

SCHEMA = {
    "type": "object",
    "properties": {
        "is_osc": {"type": "boolean"},
        "novel_osc": {"type": "boolean"},
        "energy_data_likely": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_osc", "novel_osc", "energy_data_likely", "reason"],
    "additionalProperties": False,
}

SYSTEM = f"""You screen scientific papers by their abstract for a project that collects \
HOMO/LUMO/IP/EA energy levels of organic semiconductors.

{rules_section("From each abstract", "From each paper")}

Answer three questions about the paper:
- is_osc (A1): is the paper about organic semiconductors?
- novel_osc (A2): does it report a novel organic semiconductor (synthesised or reported for \
the first time)?
- energy_data_likely (A3): is it likely to contain HOMO/LUMO/IP/EA data for an organic \
semiconductor, new or not? Papers characterising materials for organic electronics usually \
report energy levels (cyclic voltammetry, UPS, DFT); reviews, device-physics or processing \
papers often do not.
- reason: one or two sentences explaining the answers.

When only the title is available, answer from the title and say so in the reason."""


def judge(title, abstract, model=None):
    """Claude's answers for one paper: {is_osc, novel_osc, energy_data_likely, reason}."""
    text = f"Title: {title or '(unknown)'}\n\nAbstract: {abstract or '(not available; title only)'}"
    data, _ = llm.ask_json(model or llm.MODELS["triage"], SYSTEM, [{"type": "text", "text": text}],
                           SCHEMA, effort="low", max_tokens=8000, task=f"triage:{title or ''}"[:120])
    return data


def verdict(answers):
    """A1-A4 -> (status, reason)."""
    if not answers["is_osc"]:
        return IRRELEVANT, "Not organic semiconductors"
    if answers["novel_osc"] or answers["energy_data_likely"]:
        return TO_READ, answers["reason"]
    return IRRELEVANT, "No energy data expected"


def run(ledger, batch=None, model=None):
    """Triage the to_triage queue. Returns [(doi, status, reason)]."""
    sources = ledger.queue(TO_TRIAGE, batch)
    works = fetch.get_works([s["doi"] for s in sources])
    results = []
    for s in sources:
        work = works.get(s["doi"]) or {}
        title = work.get("title") or s["title"]
        answers = judge(title, work.get("abstract"), model)
        status, reason = verdict(answers)
        if status == IRRELEVANT:
            reason = f"{reason} (abstract): {answers['reason']}"
        ledger.set_status(s["doi"], status, reason, title=title)
        results.append((s["doi"], status, reason))
    return results
