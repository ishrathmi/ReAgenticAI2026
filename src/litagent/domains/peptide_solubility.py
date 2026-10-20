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


def to_one_letter(seq) -> str | None:
    """Canonical one-letter sequence, or None if the sequence has non-canonical residues."""
    if not seq:
        return None
    s = str(seq).strip()
    s = re.sub(r"^(H-|Ac-|H2N-|NH2-)", "", s)
    s = re.sub(r"(-OH|-NH2|-NH₂|-CONH2)$", "", s)
    if re.fullmatch(r"([A-Z][a-z]{2}-)+[A-Z][a-z]{2}", s):
        try:
            return "".join(THREE[r] for r in s.split("-"))
        except KeyError:
            return None
    s = re.sub(r"[\s-]+", "", s)
    return s if re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", s) else None


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


def enrich(rec: dict) -> dict:
    n_guess, c_guess = termini_from_sequence(rec.get("sequence"))
    n_term = rec.get("n_terminus") or n_guess
    c_term = rec.get("c_terminus") or c_guess
    seq1 = to_one_letter(rec.get("sequence"))
    mw = molecular_weight(seq1, n_term, c_term)
    mgml, note = to_mg_per_ml(rec.get("solubility_value"), rec.get("solubility_unit"), mw)

    reasons = []
    t = rec.get("temperature_c")
    if isinstance(t, (int, float)) and t > 200:          # reported in kelvin
        rec["temperature_c"] = round(t - 273.15, 2)
        rec["notes"] = "; ".join(filter(None, [rec.get("notes"), f"temperature converted from {t} K"]))
    if not rec.get("evidence_verified"):
        reasons.append("evidence quote not found in source")
    conf = rec.get("confidence")
    if isinstance(conf, (int, float)) and conf < 0.6:
        reasons.append("low model confidence")
    if rec.get("solubility_value") is not None and mgml is None and "kept as reported" not in note:
        reasons.append(note or "value not converted")
    if rec.get("solubility_value") is None and not rec.get("solubility_qualitative"):
        reasons.append("no value or qualitative result")
    if not rec.get("sequence"):
        reasons.append("sequence not found (not in passage or linked SI table)")

    rec.update(sequence_one_letter=seq1, calc_mw_g_mol=mw, solubility_mg_per_ml=mgml,
               conversion_note=note, needs_review=bool(reasons), review_reason="; ".join(reasons))
    return rec
