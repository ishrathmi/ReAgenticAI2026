"""Inbox sorter: which wanted paper a dropped file belongs to, and whether it is the main text or SI.

A second opinion next to fetch.py's filename/DOI rules; fetch.collect_inbox only files a
document when both agree.
"""
from .. import llm
from ..dois import clean_doi

SCHEMA = {
    "type": "object",
    "properties": {
        "doi": {"type": ["string", "null"]},
        "kind": {"type": "string", "enum": ["main", "si", "other"]},
        "reason": {"type": "string"},
    },
    "required": ["doi", "kind", "reason"],
    "additionalProperties": False,
}

SYSTEM = """You sort files that a researcher downloaded for a literature-extraction project.

For each file you get its file name, the text of its first pages, and the list of papers \
the project is waiting for (DOI and title). Decide:

- doi: the DOI of the waiting paper this file belongs to, copied exactly from the list, \
or null if it matches none of them. Match on a printed DOI, or on the title and authors \
when no DOI is printed. Never pick a paper on a guess.
- kind: "main" if the file is the published article itself; "si" if it is supporting or \
supplementary information (often titled "Supporting Information", "Electronic \
Supplementary Material" or "Supplementary Information", with figure numbers like S1); \
"other" for anything else (a different paper, a cover letter, a reference list).
- reason: one short sentence on what you based this on."""


def sort_file(filename, text, wanted, model=None):
    """wanted: list of {"doi", "title"}. Returns {"doi", "kind", "reason"}; doi is cleaned or None."""
    papers = "\n".join(f"- {w['doi']}: {w.get('title') or '(title unknown)'}" for w in wanted)
    content = [{"type": "text", "text": f"File name: {filename}\n\nPapers we are waiting for:\n{papers}\n\n"
                                        f"First pages of the file:\n{text[:8000]}"}]
    data, _ = llm.ask_json(model or llm.MODELS["inbox"], SYSTEM, content, SCHEMA,
                           effort="low", max_tokens=4000, task=f"inbox:{filename}")
    known = {clean_doi(w["doi"]) for w in wanted}
    doi = clean_doi(data["doi"]) if data["doi"] else None
    data["doi"] = doi if doi in known else None
    return data
