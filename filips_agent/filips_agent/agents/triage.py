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
- energy_data_likely (A3): is it likely to report original HOMO/LUMO/IP/EA data for an \
organic semiconductor, new or not: measured or calculated in this paper, not referenced? Papers \
characterising materials usually measure or calculate energy levels (cyclic voltammetry, UPS, \
DFT). Device, device-physics, processing and review papers that use known materials usually \
show energy levels taken from the literature (e.g. in an energy-level diagram); that is not \
original data.
- reason: one or two sentences explaining the answer that decides: what is novel, or which \
original energy data the paper is likely to report, or why neither applies.

When only the title is available, answer from the title and say so in the reason."""


def judge(title, abstract, model=None):
    """Claude's answers for one paper: {is_osc, novel_osc, energy_data_likely, reason}."""
    text = f"Title: {title or '(unknown)'}\n\nAbstract: {abstract or '(not available; title only)'}"
    data, _ = llm.ask_json(model or llm.MODELS["triage"], SYSTEM, [{"type": "text", "text": text}],
                           SCHEMA, effort="low", max_tokens=8000, task=f"triage:{title or ''}"[:120])
    return data


def verdict(answers):
    """A1-A4 -> (status, reason); the reason starts with the decision and the rule that made it."""
    why = answers["reason"]
    if not answers["is_osc"]:
        return IRRELEVANT, f"Not organic semiconductors (abstract, A1): {why}"
    grounds = [g for g, yes in (("A2, novel semiconductor", answers["novel_osc"]),
                                ("A3, original energy data likely", answers["energy_data_likely"])) if yes]
    if grounds:
        return TO_READ, f"To read ({'; '.join(grounds)}): {why}"
    return IRRELEVANT, f"No energy data expected (abstract, A4): {why}"


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
        ledger.set_status(s["doi"], status, reason, title=title)
        results.append((s["doi"], status, reason))
    return results
