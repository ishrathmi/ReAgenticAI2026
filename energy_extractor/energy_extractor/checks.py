"""Plausibility and consistency checks on W2 rows before they are stored (RULES.md W2a, W2b, W3).

Every check returns a list of issues; a compound with any issue goes to the review table
with the issue as its reason. Nothing is dropped.
"""
import re

from .normalize_values import NA

# usual ranges in eV; values outside are possible but worth a human look
RANGES = {"ip": (4.0, 7.0), "ea": (2.0, 4.5), "homo_dft": (-7.5, -3.5), "lumo_dft": (-5.0, 0.0)}
MAX_GAP_DIFF = 1.0  # eV between the DFT gap and the measured IP - EA gap

_LABEL = re.compile(r"^[A-Za-z]?\d+[a-z]?$")  # "1", "2a", "P3": compound numbers, not names


def _num(row, column):
    try:
        return float(row[column])
    except (KeyError, TypeError, ValueError):
        return None


def check_rows(rows):
    """Range checks, IP > EA, HOMO < LUMO, and DFT gap vs measured gap, per row."""
    issues = []
    for row in rows:
        v = {c: _num(row, c) for c in RANGES}
        for column, (lo, hi) in RANGES.items():
            if v[column] is not None and not lo <= v[column] <= hi:
                issues.append(f"{column} {row[column]} eV outside the usual range {lo:g} to {hi:g} eV")
        if v["ip"] is not None and v["ea"] is not None and v["ip"] <= v["ea"]:
            issues.append(f"IP {row['ip']} eV is not larger than EA {row['ea']} eV")
        if v["homo_dft"] is not None and v["lumo_dft"] is not None and v["homo_dft"] >= v["lumo_dft"]:
            issues.append(f"DFT HOMO {row['homo_dft']} eV is not below LUMO {row['lumo_dft']} eV")
        if None not in v.values():
            dft_gap, gap = v["lumo_dft"] - v["homo_dft"], v["ip"] - v["ea"]
            if abs(dft_gap - gap) > MAX_GAP_DIFF:
                issues.append(f"DFT gap {dft_gap:.2f} eV and measured gap {gap:.2f} eV differ by more "
                              f"than {MAX_GAP_DIFF:g} eV")
    return list(dict.fromkeys(issues))


def name_key(name):
    """Names compare without case, spaces or hyphens: "PC71BM" = "PC71-BM" = "pc71bm"."""
    return re.sub(r"[\s\-‐‑–]", "", name).lower()


def names_of(row):
    names = [] if row.get("name") in (None, NA) else row["name"].split(", ")
    return {name_key(n) for n in names if n.strip() and not _LABEL.match(n.strip())}


def conflicts(rows, known, doi):
    """W2a/W2b: compare a compound with records already in the data table from other papers.

    W2a: same SMILES under names that share nothing. W2b: same name with a different SMILES.
    All rows of one compound share its name and SMILES, so the first row is enough.
    """
    if not rows:
        return []
    row = rows[0]
    names, smiles = names_of(row), row.get("smiles", NA)
    issues = []
    for other in known:
        if other["source_doi"] == doi:
            continue
        theirs, their_smiles = names_of(other), other.get("smiles", NA)
        if smiles != NA and their_smiles == smiles and names and theirs and not names & theirs:
            issues.append(f"W2a: same SMILES is called {other['name']!r} in {other['source_doi']}")
        elif names & theirs and NA not in (smiles, their_smiles) and their_smiles != smiles:
            issues.append(f"W2b: {other['name']!r} has a different SMILES in {other['source_doi']}")
    return list(dict.fromkeys(issues))
