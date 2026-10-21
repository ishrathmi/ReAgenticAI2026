import csv
import random
from pathlib import Path

import pytest
from rdkit import Chem

from energy_extractor.structures import canonicalize, formula_matches

GOLD = list(csv.DictReader(open(Path(__file__).parents[1] / "eval" / "gold_labels.csv", encoding="utf-8")))
IT_M = next(r["smiles"] for r in GOLD if r["name"] == "IT-M")
SPIRO = next(r["smiles"] for r in GOLD if r["name"] == "Spiro-OMeTAD")


@pytest.mark.parametrize("row", GOLD, ids=[r["name"] for r in GOLD])
def test_gold_smiles_are_canonical(row):
    assert canonicalize(row["smiles"]) == (row["smiles"], None)


def shuffled_cxsmiles(cxsmiles, seed):
    """Same molecule and same m: attachments, written with a random atom order."""
    base, block = cxsmiles.split(" |m:")
    mol = Chem.MolFromSmiles(base)
    order = list(range(mol.GetNumAtoms()))
    random.Random(seed).shuffle(order)
    mol = Chem.RenumberAtoms(mol, order)          # new atom i = old atom order[i]
    text = Chem.MolToSmiles(mol, canonical=False)
    written = list(mol.GetPropsAsDict(includePrivate=True, includeComputed=True)["_smilesAtomOutputOrder"])
    pos = {order[atom]: i for i, atom in enumerate(written)}  # old index -> position in text
    entries = []
    for f in block.rstrip("|").split(","):
        atom, ends = f.split(":")
        entries.append(f"{pos[int(atom)]}:{'.'.join(str(pos[int(e)]) for e in ends.split('.'))}")
    return f"{text} |m:{','.join(entries)}|"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cxsmiles_m_block_survives_reordering(seed):
    shuffled = shuffled_cxsmiles(IT_M, seed)
    assert shuffled != IT_M
    assert canonicalize(shuffled) == (IT_M, None)


def test_invalid_smiles():
    assert canonicalize("c1ccc") == (None, "invalid SMILES")
    assert canonicalize("") == (None, "no SMILES")


def test_formula_check():
    assert formula_matches(SPIRO, "C81H68N4O8") is True
    assert formula_matches(SPIRO, "C81H68N4O7") is False
    assert formula_matches("c1ccsc1", "C4H5S+") is True              # [M+H]+
    assert formula_matches("c1ccsc1", "C4H4NaS") is True             # [M+Na]+
    assert formula_matches("c1ccsc1", "C4H6S") is False
    assert formula_matches("*c1ccc(*)s1", "C4H2S") is None          # polymers are not checked
    assert formula_matches("c1ccsc1", None) is None
