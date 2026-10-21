"""OPSIN/PubChem cross-checks, with fake lookups (no network)."""
import pytest

from filips_agent import resolve


@pytest.fixture
def hits(monkeypatch):
    """Fake answers per (source, name)."""
    table = {}
    monkeypatch.setattr(resolve, "lookup", lambda source, name, cache_dir=None: table.get((source, name), []))
    return table


def test_skeleton_ignores_bond_orders_charges_and_kekule_form():
    assert resolve.skeleton("c1ccccc1") == resolve.skeleton("C1=CC=CC=C1") == resolve.skeleton("C1CCCCC1")
    assert resolve.skeleton("CC(=O)[O-]") == resolve.skeleton("CC(O)O")
    assert resolve.skeleton("CCO") != resolve.skeleton("COC")
    assert resolve.skeleton("not a smiles") is None


def test_matching_lookup_confirms_silently(hits):
    hits[("PubChem", "benzene")] = [{"smiles": "C1=CC=CC=C1", "id": "PubChem CID 241"}]
    assert resolve.cross_check("c1ccccc1", ["benzene"]) == ("c1ccccc1", [], None)


def test_any_agreeing_hit_is_enough(hits):
    hits[("PubChem", "X")] = [{"smiles": "CCN", "id": "PubChem CID 1"}, {"smiles": "CCO", "id": "PubChem CID 2"}]
    assert resolve.cross_check("OCC", ["X"])[1] == []


def test_disagreeing_lookup_goes_to_review(hits):
    hits[("OPSIN", "ethanol")] = [{"smiles": "CCO", "id": "OPSIN"}]
    _, issues, _ = resolve.cross_check("COC", ["ethanol"])
    assert issues == ["structure differs from OPSIN for 'ethanol' (P3)"]


def test_nothing_found_means_unchecked(hits):
    assert resolve.cross_check("CCO", ["IT-M"]) == ("CCO", [], None)


def test_polymers_and_cxsmiles_are_skipped(hits):
    hits[("PubChem", "P3HT")] = [{"smiles": "CCCCCCc1ccsc1", "id": "PubChem CID 566849"}]  # the monomer
    assert resolve.cross_check("*c1sc(*)c(CCCCCC)c1", ["P3HT"])[1] == []
    assert resolve.cross_check("*C.c1ccccc1 |m:0:1.2|", ["P3HT"])[1] == []


def test_missing_structure_filled_from_opsin_or_pubchem(hits):
    hits[("OPSIN", "ethanol")] = [{"smiles": "OCC", "id": "OPSIN"}]
    hits[("PubChem", "EtOH")] = [{"smiles": "OCC", "id": "PubChem CID 702"}]
    assert resolve.cross_check(None, ["ethanol"]) == ("CCO", [], "structure from OPSIN ('ethanol')")
    smiles, issues, note = resolve.cross_check(None, ["EtOH"])
    assert smiles == "CCO" and "taken from PubChem CID 702 for 'EtOH'; check it" in issues[0]


def test_lookup_caches_answers_but_not_failures(tmp_path, monkeypatch):
    monkeypatch.undo()  # use the real lookup (conftest replaces it), with fake services
    calls = []

    def found(name):
        calls.append(name)
        return [{"smiles": "CCO", "id": "OPSIN"}]

    def offline(name):
        raise TimeoutError

    monkeypatch.setattr(resolve, "SOURCES", {"OPSIN": found, "PubChem": offline})
    assert resolve.lookup("OPSIN", "ethanol", tmp_path) == [{"smiles": "CCO", "id": "OPSIN"}]
    assert resolve.lookup("OPSIN", "ethanol", tmp_path) == [{"smiles": "CCO", "id": "OPSIN"}]
    assert calls == ["ethanol"]
    assert resolve.lookup("PubChem", "ethanol", tmp_path) is None
    assert not (tmp_path / "PubChem").exists()
