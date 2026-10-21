"""Range, gap and W2a/W2b checks."""
import pytest

from filips_agent.checks import check_rows, conflicts, name_key
from filips_agent.normalize_values import COLUMNS, NA


def row(**kw):
    return {c: NA for c in COLUMNS} | {"name": "IT-M", "smiles": "CCO", "source_doi": "10.1000/a"} | kw


def test_plausible_row_passes():
    assert check_rows([row(homo_dft="-5.50", lumo_dft="-3.35", ip="5.58", ea="3.98")]) == []


@pytest.mark.parametrize("kw, issue", [
    ({"ip": "7.90"}, "ip 7.90 eV outside the usual range 4 to 7 eV"),
    ({"ea": "1.20"}, "ea 1.20 eV outside"),
    ({"lumo_dft": "0.40"}, "lumo_dft 0.40 eV outside"),
    ({"ip": "5.00", "ea": "5.10"}, "IP 5.00 eV is not larger than EA 5.10 eV"),
    ({"homo_dft": "-3.90", "lumo_dft": "-4.10"}, "DFT HOMO -3.90 eV is not below LUMO -4.10 eV"),
    ({"homo_dft": "-5.50", "lumo_dft": "-1.00", "ip": "5.60", "ea": "3.90"}, "differ by more than 1 eV"),
])
def test_implausible_values_are_flagged(kw, issue):
    assert any(issue in i for i in check_rows([row(**kw)])), check_rows([row(**kw)])


def test_gap_check_only_where_all_four_values_exist():
    # DFT on the first row only (W2c); the second measurement set has no DFT to compare with
    assert check_rows([row(homo_dft="-5.5", lumo_dft="-3.3", ip="5.6", ea="3.5"), row(ip="5.0", ea="2.2")]) == []


def test_name_key_ignores_case_spaces_and_hyphens():
    assert name_key("PC71-BM") == name_key("pc71bm") == name_key("PC 71BM")


def test_w2a_same_smiles_different_names():
    known = [row(name="ITIC-M", source_doi="10.1000/b")]
    assert conflicts([row()], known, "10.1000/a") == ["W2a: same SMILES is called 'ITIC-M' in 10.1000/b"]
    assert conflicts([row(name="IT-M, ITIC-M")], known, "10.1000/a") == []  # a shared synonym is fine


def test_w2b_same_name_different_smiles():
    known = [row(smiles="CCN", source_doi="10.1000/b")]
    assert conflicts([row(name="it-m")], known, "10.1000/a") == ["W2b: 'IT-M' has a different SMILES in 10.1000/b"]


def test_no_conflict_with_own_paper_or_compound_numbers():
    assert conflicts([row()], [row(smiles="CCN")], "10.1000/a") == []  # same paper (re-extraction)
    assert conflicts([row(name="1b")], [row(name="1b", smiles="CCN", source_doi="10.1000/b")], "10.1000/a") == []
