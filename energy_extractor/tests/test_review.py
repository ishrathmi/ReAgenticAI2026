"""Decisions typed into review.csv, and how the ledger keeps them."""
import csv
import sqlite3

import pytest

from energy_extractor import review
from energy_extractor.agents import extractor
from energy_extractor.ledger import DONE, IRRELEVANT, PARTIAL, REVIEW, TO_READ, Ledger
from energy_extractor.normalize_values import normalize_record


def compound(name="X1", smiles="c1ccsc1", ip=5.4):
    raw = {"names": [name], "smiles": smiles, "source_doi": "10.1/a",
           "values": [{"label": "IP", "value": ip, "origin": "measured", "technique": "UPS"}]}
    return normalize_record(raw)


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as lg:
        lg.add("10.1/a")
        yield lg


@pytest.fixture
def sheet(ledger, tmp_path):
    """Export review.csv, let the test fill in rows, apply it."""
    def fill(**rows):
        out = ledger.export(tmp_path / "out")
        path = out / "review.csv"
        with open(path, encoding="utf-8-sig") as f:
            table = list(csv.DictReader(f))
        for r in table:
            r.update(rows.get(r["name"], {}))
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, table[0].keys())
            w.writeheader()
            w.writerows(table)
        return review.apply(ledger, path)
    return fill


def flagged(ledger, *names):
    ledger.save_compounds("10.1/a", [(rows, ["W2b: check me"]) for rows, _ in (compound(n) for n in names)])


def test_accept_moves_record_to_data(ledger, sheet):
    flagged(ledger, "X1")
    assert sheet(X1={"decision": "accept"}) == ["#1 X1: accepted"]
    [rec] = ledger.records("data")
    assert rec["reviewed"] == "accepted" and ledger.get("10.1/a")["status"] == DONE


def test_accept_with_edits_canonicalises_and_ignores_excel_number_format(ledger, sheet):
    flagged(ledger, "X1")
    msgs = sheet(X1={"decision": "A", "smiles": "C1=CSC=C1", "ip": "5.4", "ip_method": "UPS film"})
    assert msgs == ["#1 X1: corrected (ip_method)"]  # same SMILES and same number: not edits
    [rec] = ledger.records("data")
    assert (rec["smiles"], rec["ip"], rec["ip_method"]) == ("c1ccsc1", "5.40", "UPS film")


def test_bad_edit_is_not_applied_and_explained(ledger, sheet):
    flagged(ledger, "X1")
    [msg] = sheet(X1={"decision": "accept", "smiles": "c1ccc", "note": "fixed ring"})
    assert "could not apply your accept: smiles: invalid SMILES" in msg
    [rec] = ledger.records("review")
    assert "your note: fixed ring" in rec["review_note"]


def test_implausible_edit_is_applied_with_a_warning(ledger, sheet):
    flagged(ledger, "X1")
    msgs = sheet(X1={"decision": "accept", "ip": "7.9"})
    assert any("outside the usual range" in m for m in msgs)
    assert ledger.records("data")[0]["ip"] == "7.90"


def test_reject_and_status(ledger, sheet, tmp_path):
    flagged(ledger, "X1", "X2")
    sheet(X1={"decision": "reject", "note": "cited value"})
    assert ledger.get("10.1/a")["status"] == REVIEW
    sheet(X2={"decision": "accept"})
    assert ledger.get("10.1/a")["status"] == DONE
    with open(ledger.export(tmp_path / "out") / "rejected.csv", encoding="utf-8") as f:
        [row] = list(csv.DictReader(f))
    assert (row["name"], row["review_note"]) == ("X1", "cited value")


def test_everything_rejected_makes_paper_irrelevant(ledger, sheet):
    flagged(ledger, "X1")
    sheet(X1={"decision": "reject"})
    assert ledger.get("10.1/a")["status"] == IRRELEVANT


def test_reread_puts_paper_back_with_a_hint_for_the_extractor(ledger, sheet, tmp_path):
    flagged(ledger, "X1", "X2")
    sheet(X1={"decision": "reread", "note": "EA is in Table S2"}, X2={"decision": "accept"})
    assert ledger.get("10.1/a")["status"] == TO_READ
    assert ledger.hints("10.1/a") == ["EA is in Table S2"]
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    blocks = extractor._content(pdf, [], False, "10.1/a", "T", [], False, hints=ledger.hints("10.1/a"))
    assert "- EA is in Table S2" in blocks[-1]["text"]


def test_unknown_decision_rows_already_applied_and_no_file(ledger, sheet, tmp_path):
    flagged(ledger, "X1")
    assert sheet(X1={"decision": "maybe"}) == ["#1 X1: unknown decision 'maybe' (use accept, reject or reread)"]
    assert sheet(X1={"decision": "accept"}) == ["#1 X1: accepted"]
    assert review.apply(ledger, tmp_path / "out" / "review.csv") == []  # rewritten: nothing pending
    assert review.apply(ledger, tmp_path / "missing.csv") == []


def test_decisions_survive_reading_the_paper_again(ledger, sheet):
    flagged(ledger, "X1", "X2")
    sheet(X1={"decision": "accept", "ip": "5.50"}, X2={"decision": "reject"})
    # a new reading finds both compounds again (X1 with its old value) plus a new one
    ledger.save_compounds("10.1/a", [compound("X1"), compound("X2"), compound("X3", smiles="c1ccoc1")])
    assert [(r["name"], r["ip"]) for r in ledger.records("data")] == [("X1", "5.50"), ("X3", "5.40")]
    assert [r["name"] for r in ledger.records("rejected")] == ["X2"]
    assert ledger.get("10.1/a")["status"] == DONE


def test_sync_names_for_same_smiles():
    with Ledger(":memory:") as lg:
        lg.add("10.1/a")
        lg.add("10.1/b")
        lg.save_compounds("10.1/a", [compound("PNDI2OD-T2")])
        rows, _ = compound("P(NDI2OD-T2)")
        lg.save_compounds("10.1/b", [([dict(r, source_doi="10.1/b") for r in rows], [])])
        assert lg.sync_names() == 2
        assert {r["name"] for r in lg.records("data")} == {"PNDI2OD-T2, P(NDI2OD-T2)"}


def test_old_ledger_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE sources (doi TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, likelihood TEXT,
            found_in TEXT, reason TEXT, added_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE records (id INTEGER PRIMARY KEY, source_doi TEXT NOT NULL,
            target TEXT NOT NULL CHECK (target IN ('data', 'review')), review_reason TEXT,
            name TEXT, smiles TEXT, homo_dft TEXT, lumo_dft TEXT, dft_method TEXT, ip TEXT, ip_method TEXT,
            ea TEXT, ea_method TEXT, comment TEXT);
        INSERT INTO sources VALUES ('10.1/a', NULL, 'partial', NULL, NULL, NULL, 0, 0);
        INSERT INTO records (source_doi, target, name, smiles) VALUES ('10.1/a', 'review', 'X1', 'C');
    """)
    db.close()
    with Ledger(path) as lg:
        [rec] = lg.records("review")
        assert rec["name"] == "X1" and rec["reviewed"] is None
        lg.update_record(rec["id"], target="rejected")
        assert lg.refresh_status("10.1/a") == IRRELEVANT


def test_partial_paper_status_after_review(ledger, sheet):
    ledger.save_compounds("10.1/a", [compound("X1"), (compound("X2")[0], ["check"])])
    assert ledger.get("10.1/a")["status"] == PARTIAL
