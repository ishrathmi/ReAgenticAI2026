"""Post-processing for peptide solubility records: sequence parsing, MW, conversion to mg/mL.

A domain module is optional. It exposes enrich(record) -> record, called after extraction.
"""

from __future__ import annotations

import re

# Average residue masses (amino acid minus H2O), g/mol
RESIDUE_MASS = {
    "A": 71.0788, "R": 156.1875, "N": 114.1038, "D": 115.0886, "C": 103.1388,
    "E": 129.1155, "Q": 128.1307, "G": 57.0519, "H": 137.1411, "I": 113.1594,
    "L": 113.1594, "K": 128.1741, "M": 131.1926, "F": 147.1766, "P": 97.1167,
    "S": 87.0782, "T": 101.1051, "W": 186.2132, "Y": 163.1760, "V": 99.1326,
}
WATER = 18.0153
N_TERM = {"free": 0.0, "h": 0.0, "nh2": 0.0, "acetyl": 42.0367, "ac": 42.0367, "acetylated": 42.0367}
C_TERM = {"free": 0.0, "oh": 0.0, "acid": 0.0, "amide": -0.9848, "nh2": -0.9848, "amidated": -0.9848}
THREE = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C", "Glu": "E", "Gln": "Q",
    "Gly": "G", "His": "H", "Ile": "I", "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F",
    "Pro": "P", "Ser": "S", "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V",
}
MASS_UNITS = {   # factor to mg/mL
    "mg/ml": 1.0, "mg ml-1": 1.0, "mg·ml-1": 1.0, "g/l": 1.0, "g l-1": 1.0, "mg/cm3": 1.0,
    "µg/ml": 1e-3, "ug/ml": 1e-3, "µg ml-1": 1e-3, "mg/l": 1e-3, "mg l-1": 1e-3,
    "ng/ml": 1e-6, "g/ml": 1e3, "g/100 ml": 10.0,
}
MOLAR_UNITS = {  # factor to mol/L
    "m": 1.0, "mol/l": 1.0, "mm": 1e-3, "mmol/l": 1e-3,
    "µm": 1e-6, "um": 1e-6, "µmol/l": 1e-6, "nm": 1e-9,
}
PERCENT_UNITS = {"% w/v", "%w/v", "% (w/v)", "wt% (w/v)"}
NON_CONVERTIBLE = {"mole fraction", "x", "mol fraction", "mol/kg", "mol kg-1", "g/kg", "mg/g", "log s", "logs"}


THREE_CODE_RE = re.compile(r"[A-Za-z]{3}(?:\s*-\s*[A-Za-z]{3})+")


def _three_letter(s: str) -> str | None:
    """'Ala-Gly', 'ala-ala', 'GLY-GLY' -> 'AG', 'AA', 'GG'; None if any code is unknown."""
    codes = [c.strip().capitalize() for c in s.split("-")]
    if all(c in THREE for c in codes):
        return "".join(THREE[c] for c in codes)
    return None


def to_one_letter(seq) -> str | None:
    """Canonical one-letter sequence, or None if the sequence has non-canonical residues.

    Accepts one-letter ('KLVFFAE'), three-letter in any case ('Ala-Gly', 'ala-ala'),
    caps ('Ac-...-NH2', 'H-...-OH'), and labels with a sequence in brackets ('AA (ala-ala)').
    """
    if not seq:
        return None
    s = str(seq).strip()
    bracket = re.search(r"\(([^()]+)\)", s)
    if bracket and THREE_CODE_RE.fullmatch(bracket.group(1).strip()):
        s = bracket.group(1).strip()
    s = re.sub(r"^(H-|Ac-|H2N-|NH2-)", "", s, flags=re.I)
    s = re.sub(r"(-OH|-NH2|-NH₂|-CONH2)$", "", s, flags=re.I)
    if THREE_CODE_RE.fullmatch(s):
        one = _three_letter(s)
        if one:
            return one
    s = re.sub(r"[\s-]+", "", s)
    return s if re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", s) else None


def sequence_from_name(name) -> str | None:
    """Use the peptide name as the sequence only if it is written in three-letter code."""
    if not name:
        return None
    s = str(name)
    m = THREE_CODE_RE.search(s)
    return m.group(0) if m and _three_letter(re.sub(r"\s+", "", m.group(0))) else None


def termini_from_sequence(seq) -> tuple:
    s = str(seq or "")
    n = "acetyl" if s.startswith("Ac-") else None
    c = "amide" if re.search(r"-(NH2|NH₂|CONH2)$", s) else ("free" if s.endswith("-OH") else None)
    return n, c


def molecular_weight(seq1, n_term, c_term) -> float | None:
    if not seq1:
        return None
    n = N_TERM.get(str(n_term or "free").lower())
    c = C_TERM.get(str(c_term or "free").lower())
    if n is None or c is None:
        return None                     # unusual cap: do not guess its mass
    return round(sum(RESIDUE_MASS[a] for a in seq1) + WATER + n + c, 2)


def _unit(u: str) -> str:
    u = str(u).strip().replace("−", "-").replace("–", "-").replace("⁻¹", "-1").replace("μ", "µ")
    return re.sub(r"\s+", " ", u).lower()


def _sig(x: float, n: int = 4) -> float:
    return float(f"{x:.{n}g}")


def to_mg_per_ml(value, unit, mw) -> tuple:
    if value is None or not unit:
        return None, ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, "value is not numeric"
    u = _unit(unit)
    if u in MASS_UNITS:
        return _sig(v * MASS_UNITS[u]), ""
    if u in MOLAR_UNITS:
        if mw is None:
            return None, "molar unit; MW not calculable from sequence"
        return _sig(v * MOLAR_UNITS[u] * mw), "converted from molar using calculated MW"
    if u in PERCENT_UNITS:
        return _sig(v * 10.0), "w/v %: 1% = 10 mg/mL"
    if u in NON_CONVERTIBLE:
        return None, f"'{unit}' kept as reported; conversion needs solvent density"
    return None, f"unit '{unit}' not recognised"


# Statements that a peptide is C-terminally amidated. Plain "amide" is excluded: it usually means a backbone amide bond.
C_AMIDE_RE = re.compile(r"C-?\s?amidated|C-?\s?terminal(?:ly)?\s+amid\w*|\w-NH2\b|\w-NH₂|CONH2|Rink\s+amide", re.I)
# A unit in the evidence quote: mass/volume, molar, w/v %, mole fraction
UNIT_IN_TEXT_RE = re.compile(r"\b(?:[mµμun]?g)\s*(?:/|·|\s)\s*(?:m?L|dL|cm3)\b|(?-i:\b[mµμun]?M\b)|mol\s*/\s*L|%\s*\(?w/v|mole?\s+fraction", re.I)


def paper_flags(text: str) -> dict:
    """Facts about the whole paper that a single passage may not contain."""
    return {"paper_mentions_c_amide": bool(C_AMIDE_RE.search(text or ""))}


# A peptide sequence written in a passage: three-letter code (Ala-Gly) or six or more one-letter residues
SEQUENCE_IN_TEXT_RE = re.compile(r"\b[A-Za-z]{3}(?:-[A-Za-z]{3})+\b|\b[ACDEFGHIKLMNPQRSTVWY]{6,}\b")
COSOLVENT_RE = re.compile(r"\b(DMSO|ethanol|EtOH|methanol|MeOH|acetonitrile|MeCN|HFIP|TFE|DMF|glycerol|"
                          r"dioxane|isopropanol|IPA|acetone)\b", re.I)
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")


def passage_flags(text: str) -> dict:
    """Facts about the passage a record came from, kept with the record so checks can be re-run later."""
    text = text or ""
    seq = any(_is_sequence(m.group(0)) for m in SEQUENCE_IN_TEXT_RE.finditer(text))
    return {"passage_has_sequence": seq,
            "passage_has_cosolvent_percent": bool(COSOLVENT_RE.search(text) and PERCENT_RE.search(text))}


def _is_sequence(s: str) -> bool:
    return bool(_three_letter(s)) if "-" in s else to_one_letter(s) is not None


def paper_context(text: str, limit: int = 6) -> str:
    """Sentences from the whole paper that a repair may need (here: statements about C-terminal amidation)."""
    out = []
    for m in C_AMIDE_RE.finditer(text or ""):
        start = max(text.rfind(".", 0, m.start()) + 1, m.start() - 200)
        end = text.find(".", m.end())
        end = min(end + 1 if end != -1 else len(text), m.end() + 200)
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        if snippet not in out:
            out.append(snippet)
        if len(out) >= limit:
            break
    return "\n".join(out)


# Repair rules: review_reason fragment -> (fields the repair may change, instruction for the model)
REPAIRS = {
    "not found in source text": (
        ["solubility_unit"],
        "The extracted unit does not appear in the passage. Give the unit exactly as written in the "
        "passage (including a table header or caption), or null if the passage states no unit."),
    "evidence contains a unit but solubility_unit is empty": (
        ["solubility_unit"],
        "The evidence quote contains a unit but solubility_unit is empty. Give the unit exactly as written."),
    "passage contains a peptide sequence but sequence is empty": (
        ["sequence"],
        "The passage contains a peptide sequence (for example in a table header or caption, such as "
        "'AA (ala-ala)'). Give the sequence of THIS peptide exactly as written, and quote where it appears "
        "in repair_evidence. If no sequence in the passage belongs to this peptide, keep null."),
    "solvent is a mixture but its composition is missing": (
        ["solvent"],
        "The solvent is a mixture, and the passage states its composition for each row or condition. Give "
        "the solvent with the composition for THIS record, e.g. \"water/DMSO, 20% DMSO by mass\", and quote "
        "the row or sentence in repair_evidence."),
    "paper mentions C-terminal amidation but c_terminus is empty": (
        ["c_terminus", "n_terminus"],
        "The paper describes C-terminal amidation (see PAPER CONTEXT). Decide whether it applies to THIS "
        "peptide. If the text states it does, set c_terminus to \"amide\" (and n_terminus if also stated) and "
        "quote the supporting sentence in repair_evidence. If it refers to other peptides, keep null."),
}


def enrich(rec: dict) -> dict:
    if not rec.get("sequence"):
        from_name = sequence_from_name(rec.get("peptide_name"))
        if from_name:
            rec["sequence"] = from_name
            rec["sequence_source"] = "parsed from peptide name"
    n_guess, c_guess = termini_from_sequence(rec.get("sequence"))
    n_term = rec.get("n_terminus") or n_guess
    c_term = rec.get("c_terminus") or c_guess
    seq1 = to_one_letter(rec.get("sequence"))
    mw = molecular_weight(seq1, n_term, c_term)
    mgml, note = to_mg_per_ml(rec.get("solubility_value"), rec.get("solubility_unit"), mw)
    conversion_failed = rec.get("solubility_value") is not None and mgml is None \
        and bool(rec.get("solubility_unit")) and "kept as reported" not in note
    failure_note = note
    if mgml is not None and "molar" in note and not (n_term and c_term):
        note += "; MW assumes free termini (not stated)"

    # needs_review: the record is probably wrong. data_caveats: the source itself is incomplete.
    reasons, caveats = [], []
    t = rec.get("temperature_c")
    if isinstance(t, (int, float)) and t > 200:          # reported in kelvin
        rec["temperature_c"] = round(t - 273.15, 2)
        rec["notes"] = "; ".join(filter(None, [rec.get("notes"), f"temperature converted from {t} K"]))
    if not rec.get("evidence_verified"):
        reasons.append("evidence quote not found in source")
    conf = rec.get("confidence")
    if isinstance(conf, (int, float)) and conf < 0.6:
        reasons.append("low model confidence")
    if conversion_failed:
        reasons.append(failure_note or "value not converted")
    if rec.get("solubility_value") is not None and not rec.get("solubility_unit"):
        if UNIT_IN_TEXT_RE.search(str(rec.get("evidence") or "")):
            reasons.append("evidence contains a unit but solubility_unit is empty")
        else:
            caveats.append("unit not stated by source")
    if rec.get("unit_in_source") is False:
        reasons.append(f"unit '{rec.get('solubility_unit')}' not found in source text")
    if not c_term and (C_AMIDE_RE.search(" ".join(str(rec.get(k) or "") for k in ("evidence", "peptide_name", "sequence")))
                       or rec.get("paper_mentions_c_amide")):
        reasons.append("paper mentions C-terminal amidation but c_terminus is empty")
    if rec.get("solubility_value") is None and not rec.get("solubility_qualitative"):
        reasons.append("no value or qualitative result")
    if not rec.get("sequence"):
        if rec.get("passage_has_sequence"):
            reasons.append("passage contains a peptide sequence but sequence is empty")
        else:
            caveats.append("sequence not stated in passage or linked SI table")
    solvent = str(rec.get("solvent") or "")
    is_mixture = bool(re.search(r"/|mixture|\bmix\b", solvent, re.I) or len(COSOLVENT_RE.findall(solvent)) > 1
                      or (COSOLVENT_RE.search(solvent) and re.search(r"\bwater\b", solvent, re.I)))
    if is_mixture and not re.search(r"\d", solvent) and rec.get("passage_has_cosolvent_percent"):
        reasons.append("solvent is a mixture but its composition is missing")

    rec.update(sequence_one_letter=seq1, calc_mw_g_mol=mw, solubility_mg_per_ml=mgml,
               conversion_note=note, needs_review=bool(reasons), review_reason="; ".join(reasons),
               data_caveats="; ".join(caveats))
    return rec
