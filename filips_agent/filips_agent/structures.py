"""Structure checks with RDKit (RULES.md P3, P3a, P3d, P3e).

canonicalize() keeps CXSMILES multicentre blocks (|m:...|), which RDKit's own writer drops:
the SMILES part is canonicalised and the m: atom indices are renumbered to match.
OPSIN / PubChem / image-recognition cross-checks come later (PLAN.md step 4).
"""
import re

from rdkit import Chem, RDLogger
from rdkit.Chem.rdMolDescriptors import CalcMolFormula

RDLogger.DisableLog("rdApp.*")


def canonicalize(smiles):
    """Return (canonical SMILES or CXSMILES, None) or (None, reason it failed)."""
    smiles = (smiles or "").strip()
    if not smiles:
        return None, "no SMILES"
    base, _, cx = smiles.partition(" |")
    cx = cx.rstrip("|").strip() if cx else ""
    fields = [f for f in cx.split(",") if f] if cx else []
    # m: blocks are "m:atom:a.b.c", comma-separated with further entries "atom:a.b"
    if cx and not cx.startswith("m:"):
        mol = Chem.MolFromSmiles(smiles)
        return (smiles, None) if mol else (None, "invalid CXSMILES")
    mol = Chem.MolFromSmiles(base)
    if mol is None:
        return None, "invalid SMILES"
    canon = Chem.MolToSmiles(mol)
    if not fields:
        return canon, None
    order = list(mol.GetPropsAsDict(includePrivate=True, includeComputed=True)["_smilesAtomOutputOrder"])
    new_index = {old: new for new, old in enumerate(order)}
    entries = []
    for f in [fields[0][2:]] + fields[1:]:
        try:
            atom, ends = f.split(":")
            entries.append((new_index[int(atom)], sorted(new_index[int(e)] for e in ends.split("."))))
        except (ValueError, KeyError):
            return None, f"cannot read CXSMILES block {cx!r}"
    # Symmetry-equivalent fragments (e.g. two identical *C groups) can swap places between
    # inputs; give their attachment sets out in sorted order so the result is unique.
    ranks = list(Chem.CanonicalRankAtoms(Chem.MolFromSmiles(canon), breakTies=False))
    groups = {}
    for atom, ends in entries:
        groups.setdefault(ranks[atom], []).append((atom, ends))
    entries = []
    for group in groups.values():
        atoms = sorted(a for a, _ in group)
        entries += zip(atoms, sorted(ends for _, ends in group))
    entries.sort()
    block = ",".join(f"{a}:{'.'.join(map(str, ends))}" for a, ends in entries)
    out = f"{canon} |m:{block}|"
    if Chem.MolFromSmiles(out) is None:
        return None, "invalid CXSMILES after renumbering"
    return out, None


def is_polymer(smiles):
    return "*" in (smiles or "")


def formula_of(smiles):
    mol = Chem.MolFromSmiles(smiles)
    return CalcMolFormula(mol) if mol else None


def _counts(formula):
    counts = {}
    for element, n in re.findall(r"([A-Z][a-z]?)(\d*)", formula.replace(" ", "")):
        counts[element] = counts.get(element, 0) + (int(n) if n else 1)
    return counts


def formula_matches(smiles, reported):
    """P3e: does the reported molecular formula match the structure? None if it can't be checked.

    Accepts the neutral formula, or an HRMS ion with one extra H, Na or K ([M+H]+ etc.).
    """
    if not reported or is_polymer(smiles):
        return None
    computed = formula_of(smiles)
    if computed is None:
        return None
    want = _counts(computed)
    got = _counts(re.sub(r"[\[\]+\-·]", "", reported))
    if got == want:
        return True
    for adduct in ("H", "Na", "K"):
        if got.get(adduct, 0) >= 1:
            less = dict(got, **{adduct: got[adduct] - 1})
            if {k: v for k, v in less.items() if v} == want:
                return True
    return False
