"""Full-paper extraction (RULES.md P1-P9): compounds, energy values and promising references.

Claude reads the PDF itself (text, tables and figures) and reports values exactly as
printed; code then checks structures and evidence, converts values (normalize_values) and
sorts each compound into the data or review table (ledger.save_compounds).

The main text is read first; the SI is only sent in a second pass when Claude says the
main text isn't enough (P8). Every raw answer is kept in data/extractions/ for auditing,
and reprocess() rebuilds a paper's records from it without calling Claude.
"""
import json
import re
from pathlib import Path

from .. import checks, fetch, llm, resolve
from ..dois import clean_doi, doi_slug
from ..ledger import DATA_DIR, IRRELEVANT
from ..normalize_values import normalize_record
from ..structures import canonicalize, formula_matches
from . import rules_section

EXTRACTIONS = DATA_DIR / "extractions"

_NULLABLE_STR = {"type": ["string", "null"]}
VALUE = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["HOMO", "LUMO", "IP", "EA"]},
        "value": {"type": "number"},
        "unit": {"type": "string", "enum": ["eV", "meV", "kJ/mol"]},
        "origin": {"type": "string", "enum": ["dft", "measured"]},
        "dft_method": _NULLABLE_STR,
        "technique": _NULLABLE_STR,
        "reference": {"anyOf": [{"type": "string", "enum": ["ferrocene", "electrode"]}, {"type": "null"}]},
        "fc_anchor": {"type": ["number", "null"]},
        "plus_eg": {"type": "boolean"},
        "set": _NULLABLE_STR,
        "detail": _NULLABLE_STR,
        "original": {"type": "boolean"},
        "from_figure": {"type": "boolean"},
        "quote": {"type": "string"},
        "location": {"type": "string"},
    },
    "required": ["label", "value", "unit", "origin", "dft_method", "technique", "reference", "fc_anchor",
                 "plus_eg", "set", "detail", "original", "from_figure", "quote", "location"],
    "additionalProperties": False,
}
COMPOUND = {
    "type": "object",
    "properties": {
        "names": {"type": "array", "items": {"type": "string"}},
        "label_in_paper": _NULLABLE_STR,
        "smiles": _NULLABLE_STR,
        "structure_source": {"type": "string", "enum": ["name", "figure", "well-known", "other"]},
        "structure_confident": {"type": "boolean"},
        "structure_note": _NULLABLE_STR,
        "reported_formula": _NULLABLE_STR,
        "values": {"type": "array", "items": VALUE},
    },
    "required": ["names", "label_in_paper", "smiles", "structure_source", "structure_confident",
                 "structure_note", "reported_formula", "values"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "is_osc": {"type": "boolean"},
        "needs_si": {"type": "boolean"},
        "needs_si_reason": _NULLABLE_STR,
        "uncertain": _NULLABLE_STR,
        "compounds": {"type": "array", "items": COMPOUND},
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doi": {"type": "string"},
                    "likelihood": {"type": "string", "enum": ["likely", "possible"]},
                    "sentence": {"type": "string"},
                },
                "required": ["doi", "likelihood", "sentence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_osc", "needs_si", "needs_si_reason", "uncertain", "compounds", "references"],
    "additionalProperties": False,
}

SYSTEM = f"""You extract energy-level data on organic semiconductors from scientific papers, \
following the rules below exactly. You receive the paper as a PDF (and sometimes its \
supporting information), so you can read the text, tables, schemes and figures.

{rules_section("From each paper", "Workflow")}

How to report (code does the conversions, so report values as printed):
- is_osc (P1): whether organic semiconductors are discussed and investigated.
- compounds: one entry per organic semiconductor with at least one original HOMO, LUMO, IP or \
EA value (P2, P2a). Do not include compounds whose values are only cited from other work.
  - names: every name or abbreviation used for it (P4, P4a); empty if it is only numbered. \
label_in_paper: its number or label (e.g. "1c"), or null.
  - smiles: your SMILES for the full structure, side chains included; for polymers the repeat \
unit with * end markers; CXSMILES (e.g. |m:...|) only for ambiguous regiochemistry (P3, P3d). \
structure_source: where the structure came from. structure_confident: false if anything about \
the structure is unclear (P3c); structure_note: what is unclear. For an isomer mixture that \
CXSMILES can't express (P3d), give a representative isomer, keep structure_confident true if \
nothing else is unclear, and say in structure_note that it is a mixture and which isomer is shown.
  - reported_formula: the molecular formula the paper reports for it (characterisation, HRMS \
"calcd for", elemental analysis), or null.
  - values: one entry per reported number, exactly as printed:
    - label: what the paper calls it (HOMO, LUMO, IP or EA); value: the number with its \
printed sign; unit as printed.
    - origin: "dft" for computed values (P5), "measured" for experimental ones. DFT-computed \
IP/EA are not collected (P5c).
    - dft_method: functional/basis set, e.g. "B3LYP/6-31G(d,p)", plus anything unusual about \
the calculation (P5); not the software package. Null for measured values.
    - technique: the measurement (e.g. "CV", "UPS", "IPES", "PESA") or null when no method is \
stated (P6g). reference: for CV, "ferrocene" if Fc/Fc+ was the internal reference or \
calibration, "electrode" if only an electrode such as Ag/Ag+ was used, null if not stated \
(check figures, captions, the experimental section and SI first, P6b). fc_anchor: the energy \
the paper assigns to Fc/Fc+ vs vacuum (e.g. 4.8, 5.1), or null if not stated.
    - plus_eg: true if the value was derived from another value plus/minus the optical gap (P6d).
    - set: a short label for the measurement this value belongs to (e.g. "film", "solution", \
"UPS"); values from the same experiment share a set, so IP and EA from one CV measurement \
(including an EA derived with Eg) share it. Use null if the compound has only one measurement.
    - detail: method details that matter (P6h): onset/half-wave/peak, film/solution, \
solvent and electrolyte, temperature; null if none.
    - original: false if the value is quoted from another source (P2a).
    - from_figure: true if you read the value from a figure rather than text or a table.
    - quote: the sentence or table cell where the value is printed, copied exactly; for a \
figure, its caption. location: page, table or figure, and "SI" if from the supporting \
information.
- uncertain (P7c): a short note if the paper should be read with great scepticism, else null.
- needs_si: true if the main text points to the SI for values, the CV reference, structures or \
formulas needed here and the SI was not attached; needs_si_reason says what is missing.
- references (P9): cited works that may hold HOMO/LUMO/IP/EA data on organic semiconductors. \
Give the DOI (from the reference list you are given, or printed in the paper); skip works \
whose DOI you cannot find. likelihood: "likely" if data is attributed to that work, \
"possible" if it is mentioned in passing. sentence: the citing sentence.

Never invent values, structures or DOIs. If a structure cannot be determined, still report \
the compound with smiles null."""


def _content(main_pdf, si_paths, si_available, doi, title, references, cache_main, hints=()):
    blocks = [llm.pdf_block(main_pdf, cache=cache_main)]
    for p in si_paths:
        if p.suffix.lower() == ".pdf":
            blocks.append(llm.pdf_block(p))
        elif p.suffix.lower() == ".txt":
            blocks.append({"type": "text", "text": f"Supporting information ({p.name}):\n"
                                                   + p.read_text(encoding="utf-8", errors="replace")})
    refs = "\n".join(f"- {r['doi']}: {r.get('title') or ''}" for r in references) or "(not available)"
    if si_paths:
        si_note = "The supporting information is attached after the main paper."
    elif si_available:
        si_note = "Supporting information exists but is not attached; set needs_si if you need it."
    else:
        si_note = "No supporting information is available."
    reviewer = ("\n\nA person reviewed an earlier reading of this paper and asked for it to be read again. "
                "Their notes:\n" + "\n".join(f"- {h}" for h in hints)) if hints else ""
    blocks.append({"type": "text", "text": f"Paper: {doi}\nTitle: {title or '(unknown)'}\n{si_note}\n\n"
                                           f"Works this paper cites (DOI: title), for the references field:\n{refs}"
                                           + reviewer})
    return blocks


def _number_in_quote(value, quote):
    quote = quote.replace("−", "-").replace("–", "-")
    v = abs(float(value))
    forms = {f"{v:g}", f"{v:.1f}", f"{v:.2f}", f"{v:.3f}", str(value).lstrip("-")}
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?!\d)", quote) for f in forms)


def to_compounds(result, doi, known=()):
    """Claude's answer -> [(rows, issues)] for ledger.save_compounds.

    known: records already in the data table, for the W2a/W2b name and SMILES checks.
    """
    compounds = []
    for c in result["compounds"]:
        values = [v for v in c["values"] if v["original"]]  # P2a
        if not values:
            continue
        issues = []
        smiles, err = canonicalize(c["smiles"]) if c["smiles"] else (None, None)
        if err and c["smiles"]:
            issues.append(f"structure: {err} ({c['smiles']})")
        names = c["names"] or ([c["label_in_paper"]] if c["label_in_paper"] else [])
        smiles, lookup_issues, note = resolve.cross_check(smiles, c["names"])
        issues += lookup_issues
        if not c["structure_confident"]:
            issues.append(f"structure uncertain (P3c): {c['structure_note'] or 'no reason given'}")
        elif c["structure_note"]:  # e.g. representative isomer of a mixture (P3d)
            note = "; ".join(x for x in (c["structure_note"], note) if x)
        if smiles and formula_matches(smiles, c["reported_formula"]) is False:
            issues.append(f"reported formula {c['reported_formula']} does not match the structure (P3e)")
        raw_values = []
        for v in values:
            detail = v["detail"]
            if v["from_figure"]:
                detail = "; ".join(x for x in (f"read from {v['location']}", detail) if x)
            elif not _number_in_quote(v["value"], v["quote"]):
                issues.append(f"{v['label']} {v['value']}: number not found in its quote ({v['location']})")
            raw_values.append({k: v[k] for k in ("label", "value", "unit", "origin", "dft_method", "technique",
                                                 "reference", "fc_anchor", "plus_eg", "set")} | {"detail": detail})
        rows, more = normalize_record({"names": names, "smiles": smiles, "source_doi": doi,
                                       "comment": note, "values": raw_values})
        issues += more + checks.check_rows(rows) + checks.conflicts(rows, known, doi)
        compounds.append((rows, issues))
    return compounds


def extract(ledger, doi, model=None):
    """Read one paper with its full text in data/pdfs/ and store the results.

    Returns a summary dict, or None if the full text isn't there.
    """
    doi = clean_doi(doi)
    text = fetch.get_fulltext(doi)
    if not text:
        return None
    model = model or llm.MODELS["extract"]
    work = fetch.get_work(doi) or {}
    references = fetch.reference_list(work) if work.get("referenced_works") else []
    title = work.get("title") or (ledger.get(doi) or {}).get("title")
    has_si = bool(text["si"])

    def ask(si_paths):
        content = _content(text["main"], si_paths, has_si, doi, title, references, cache_main=has_si,
                           hints=ledger.hints(doi))
        return llm.ask_json(model, SYSTEM, content, SCHEMA, effort="high", max_tokens=64000,
                            task=f"extract:{doi}" + (":si" if si_paths else ""))

    result, usage = ask([])
    passes, cost = 1, usage["cost_usd"]
    if result["needs_si"] and has_si:
        result, usage = ask(text["si"])
        passes, cost = 2, cost + usage["cost_usd"]

    EXTRACTIONS.mkdir(parents=True, exist_ok=True)
    (EXTRACTIONS / f"{doi_slug(doi)}.json").write_text(
        json.dumps({"doi": doi, "model": model, "passes": passes, "result": result}, indent=1), encoding="utf-8")
    status, new_refs = _store(ledger, doi, result, title)
    return {"doi": doi, "status": status, "passes": passes, "cost_usd": round(cost, 4),
            "compounds": len(result["compounds"]), "new_references": new_refs}


def reprocess(ledger, doi):
    """Rebuild a paper's records from its saved extraction (after a change to the checks or
    conversions); no Claude call. Returns the new status, or None if there is no extraction."""
    doi = clean_doi(doi)
    path = EXTRACTIONS / f"{doi_slug(doi)}.json"
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    return _store(ledger, doi, saved["result"], None)[0]


def _store(ledger, doi, result, title):
    if not result["is_osc"]:
        ledger.set_status(doi, IRRELEVANT, "Not organic semiconductors (paper, P1)", title=title)
        status = IRRELEVANT
    else:
        status = ledger.save_compounds(doi, to_compounds(result, doi, ledger.records("data")), result["uncertain"])
    new_refs = 0
    for r in result["references"]:
        ref = clean_doi(r["doi"])
        if ref and ref != doi and ledger.add_reference(ref, r["likelihood"], found_in=doi):
            new_refs += 1
    return status, new_refs
