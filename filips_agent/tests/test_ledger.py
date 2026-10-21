import csv

import pytest

from filips_agent.dois import clean_doi, doi_slug, find_dois
from filips_agent.ledger import (AWAITING_PDF, DONE, IRRELEVANT, PARTIAL, REVIEW, TO_READ,
                                 TO_TRIAGE, Ledger)
from filips_agent.normalize_values import normalize_record


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


def compound(doi="10.1/a", **kw):
    raw = {"names": ["X1"], "smiles": "c1ccsc1", "source_doi": doi,
           "values": [{"label": "IP", "value": 5.4, "origin": "measured", "technique": "UPS"}], **kw}
    return normalize_record(raw)


# --- DOIs -----------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["10.1002/ADMA.201602776", "https://doi.org/10.1002/adma.201602776",
                                 "doi:10.1002/adma.201602776", " 10.1002/adma.201602776. "])
def test_clean_doi(raw):
    assert clean_doi(raw) == "10.1002/adma.201602776"


def test_doi_slug_and_find():
    assert doi_slug("10.1007/s40843-020-1639-x") == "10.1007_s40843-020-1639-x"
    text = "Adv. Mater. DOI: 10.1002/adma.201602776, see also https://doi.org/10.1038/S41560-019-0448-5."
    assert find_dois(text) == ["10.1002/adma.201602776", "10.1038/s41560-019-0448-5"]


# --- evaluate once (W7) and reference routing (W1) ---------------------------------

def test_source_is_added_once_whatever_the_spelling(ledger):
    assert ledger.add("10.1002/adma.201602776")
    assert not ledger.add("https://doi.org/10.1002/ADMA.201602776", IRRELEVANT)
    assert ledger.get("10.1002/adma.201602776")["status"] == TO_READ


def test_likely_reference_goes_to_reading_list_possible_to_triage(ledger):
    ledger.add_reference("10.1/likely", "likely", found_in="10.1/parent")
    ledger.add_reference("10.1/possible", "possible", found_in="10.1/parent")
    assert ledger.get("10.1/likely")["status"] == TO_READ
    assert ledger.get("10.1/possible")["status"] == TO_TRIAGE
    assert ledger.get("10.1/likely")["found_in"] == "10.1/parent"


def test_queue_order_seeds_then_likely_then_possible(ledger):
    ledger.add_reference("10.1/p", "possible", "10.1/x")
    ledger.set_status("10.1/p", TO_READ, "abstract relevant")
    ledger.add_reference("10.1/l", "likely", "10.1/x")
    ledger.add("10.1/seed")
    assert [s["doi"] for s in ledger.queue(TO_READ)] == ["10.1/seed", "10.1/l", "10.1/p"]
    assert len(ledger.queue(TO_READ, limit=2)) == 2


def test_bad_status_and_unknown_doi_are_errors(ledger):
    with pytest.raises(ValueError):
        ledger.add("10.1/a", "read")
    with pytest.raises(KeyError):
        ledger.set_status("10.1/unknown", DONE)


# --- per-compound sorting, per-paper status (W3) -------------------------------------

def test_all_good_compounds_make_paper_done(ledger):
    ledger.add("10.1/a")
    assert ledger.save_compounds("10.1/a", [compound(), compound()]) == DONE
    assert len(ledger.records("data")) == 2 and ledger.records("review") == []


def test_mixed_compounds_make_paper_partial(ledger):
    ledger.add("10.1/a")
    bad = compound(smiles=None)
    assert ledger.save_compounds("10.1/a", [compound(), bad]) == PARTIAL
    [rec] = ledger.records("review")
    assert rec["review_reason"] == "no structure"


def test_uncertain_paper_sends_everything_to_review(ledger):
    ledger.add("10.1/a")
    assert ledger.save_compounds("10.1/a", [compound()], uncertain="IP 7.9 eV is implausible") == REVIEW
    [rec] = ledger.records("review")
    assert rec["review_reason"] == "uncertain paper: IP 7.9 eV is implausible"
    assert ledger.get("10.1/a")["reason"] == "IP 7.9 eV is implausible"


def test_paper_without_compounds_is_irrelevant(ledger):
    ledger.add("10.1/a")
    assert ledger.save_compounds("10.1/a", []) == IRRELEVANT


def test_saving_again_replaces_records(ledger):
    ledger.add("10.1/a")
    ledger.save_compounds("10.1/a", [compound(), compound()])
    ledger.save_compounds("10.1/a", [compound()])
    assert len(ledger.records("data")) == 1


# --- exports (W4) ----------------------------------------------------------------

def test_export_writes_all_tables(ledger, tmp_path):
    ledger.add("10.1/a", title="Paper A")
    ledger.save_compounds("10.1/a", [compound()])
    ledger.add("10.1/b", IRRELEVANT, reason="Not organic semiconductors")
    ledger.add("10.1/c", AWAITING_PDF, title="Paper C", reason="no open-access PDF found")
    out = ledger.export(tmp_path / "out")

    def read(name):
        with open(out / name, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    [row] = read("data.csv")
    assert (row["name"], row["ip"], row["ip_method"], row["source_doi"]) == ("X1", "5.40", "UPS", "10.1/a")
    assert read("review.csv") == []
    assert [r["reason"] for r in read("irrelevant.csv")] == ["Not organic semiconductors"]
    assert [r["link"] for r in read("wishlist.csv")] == ["https://doi.org/10.1/c"]
    assert [r["status"] for r in read("reading_list.csv")] == [DONE, IRRELEVANT, AWAITING_PDF]
