"""Triage and extractor logic with a fake Claude (no API calls)."""
import functools
import json

import pymupdf
import pytest

from filips_agent import fetch, llm, resolve
from filips_agent.agents import extractor, triage
from filips_agent.ledger import AWAITING_SI, DONE, IRRELEVANT, PARTIAL, REVIEW, TO_READ, TO_TRIAGE, Ledger

SPIRO = ("COc1ccc(N(c2ccc(OC)cc2)c2ccc3c(c2)C2(c4cc(N(c5ccc(OC)cc5)c5ccc(OC)cc5)ccc4-3)c3cc(N(c4ccc(OC)cc4)"
         "c4ccc(OC)cc4)ccc3-c3ccc(N(c4ccc(OC)cc4)c4ccc(OC)cc4)cc32)cc1")


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


class FakeClaude:
    """Stands in for llm.ask_json: returns queued answers and records what was sent."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, model, system, content, schema, **kw):
        self.calls.append({"model": model, "content": content, "task": kw.get("task")})
        return self.answers.pop(0), {"cost_usd": 0.01}


# --- triage (A1-A4) ------------------------------------------------------------------

@pytest.mark.parametrize("answers, status, reason", [
    ({"is_osc": False, "novel_osc": False, "energy_data_likely": False}, IRRELEVANT, "Not organic semiconductors"),
    ({"is_osc": True, "novel_osc": False, "energy_data_likely": False}, IRRELEVANT, "No energy data expected"),
    ({"is_osc": True, "novel_osc": True, "energy_data_likely": False}, TO_READ, "r"),
    ({"is_osc": True, "novel_osc": False, "energy_data_likely": True}, TO_READ, "r"),
    ({"is_osc": False, "novel_osc": True, "energy_data_likely": True}, IRRELEVANT, "Not organic semiconductors"),
])
def test_triage_verdict(answers, status, reason):
    assert triage.verdict(dict(answers, reason="r")) == (status, reason)


def test_triage_run_updates_ledger(ledger, monkeypatch):
    ledger.add_reference("10.1000/a", "possible", "10.1000/parent")
    ledger.add_reference("10.1000/b", "possible", "10.1000/parent")
    monkeypatch.setattr(fetch, "get_works", lambda dois: {
        "10.1000/a": {"title": "New NFA", "abstract": "We report a new acceptor..."},
        "10.1000/b": {"title": "Perovskite stability", "abstract": None}})
    fake = FakeClaude({"is_osc": True, "novel_osc": True, "energy_data_likely": True, "reason": "new acceptor"},
                      {"is_osc": False, "novel_osc": False, "energy_data_likely": False, "reason": "perovskites"})
    monkeypatch.setattr(llm, "ask_json", fake)
    triage.run(ledger)
    assert ledger.get("10.1000/a")["status"] == TO_READ
    b = ledger.get("10.1000/b")
    assert (b["status"], b["title"]) == (IRRELEVANT, "Perovskite stability")
    assert b["reason"] == "Not organic semiconductors (abstract): perovskites"
    assert "title only" in fake.calls[1]["content"][0]["text"]
    assert ledger.queue(TO_TRIAGE) == []


# --- extractor ------------------------------------------------------------------------

def value(label, v, **kw):
    base = {"label": label, "value": v, "unit": "eV", "origin": "measured", "dft_method": None, "technique": "CV",
            "reference": "ferrocene", "fc_anchor": 4.8, "plus_eg": False, "set": None, "detail": None,
            "original": True, "from_figure": False, "quote": f"{label} of {v} eV", "location": "p. 3"}
    return base | kw


def compound(smiles=SPIRO, values=None, **kw):
    base = {"names": ["Spiro-OMeTAD"], "label_in_paper": None, "smiles": smiles, "structure_source": "well-known",
            "structure_confident": True, "structure_note": None, "reported_formula": None,
            "values": values or [value("HOMO", -5.20)]}
    return base | kw


def answer(*compounds, **kw):
    base = {"is_osc": True, "needs_si": False, "needs_si_reason": None, "uncertain": None,
            "compounds": list(compounds), "references": []}
    return base | kw


def test_good_compound_goes_to_data():
    [(rows, issues)] = extractor.to_compounds(answer(compound()), "10.1000/x")
    assert issues == []
    assert (rows[0]["name"], rows[0]["ip"], rows[0]["ip_method"]) == ("Spiro-OMeTAD", "5.20", "CV")


def test_cited_values_are_dropped_and_compound_without_original_values_skipped():
    c1 = compound(values=[value("HOMO", -5.2), value("LUMO", -2.1, original=False)])
    c2 = compound(names=["P3HT"], values=[value("HOMO", -4.9, original=False)])
    [(rows, _)] = extractor.to_compounds(answer(c1, c2), "10.1000/x")
    assert rows[0]["ea"] == "n/a"


@pytest.mark.parametrize("kw, issue", [
    ({"smiles": "c1ccc"}, "structure: invalid SMILES"),
    ({"structure_confident": False, "structure_note": "side chain unclear"}, "structure uncertain (P3c): side chain unclear"),
    ({"reported_formula": "C81H68N4O7"}, "does not match the structure (P3e)"),
    ({"values": [value("HOMO", -5.20, quote="HOMO level of -5.31 eV")]}, "number not found in its quote"),
    ({"smiles": None}, "no structure"),
])
def test_doubtful_compounds_get_issues(kw, issue):
    [(_, issues)] = extractor.to_compounds(answer(compound(**kw)), "10.1000/x")
    assert any(issue in i for i in issues), issues


def test_matching_formula_and_figure_values_pass():
    c = compound(reported_formula="C81H68N4O8",
                 values=[value("HOMO", -5.21, from_figure=True, quote="Figure 2. Energy diagram", location="Fig. 2b")])
    [(rows, issues)] = extractor.to_compounds(answer(c), "10.1000/x")
    assert issues == []
    assert rows[0]["comment"] == "IP: read from Fig. 2b"


def test_lookup_disagreement_range_and_w2b_conflicts_go_to_review(monkeypatch):
    monkeypatch.setattr(resolve, "lookup", lambda source, name, cache_dir=None:
                        [{"smiles": "CCO", "id": "PubChem CID 702"}] if source == "PubChem" else [])
    known = [{"name": "Spiro-OMeTAD", "smiles": "CCN", "source_doi": "10.1000/other"}]
    c = compound(values=[value("HOMO", -7.90)])
    [(_, issues)] = extractor.to_compounds(answer(c), "10.1000/x", known)
    assert any("structure differs from PubChem CID 702" in i for i in issues)
    assert any("ip 7.90 eV outside the usual range" in i for i in issues)
    assert any(i.startswith("W2b: 'Spiro-OMeTAD'") for i in issues)


def test_representative_isomer_note_goes_to_comment():
    c = compound(structure_note="mixture of regioisomers; trans-3 isomer shown")
    [(rows, issues)] = extractor.to_compounds(answer(c), "10.1000/x")
    assert issues == []
    assert rows[0]["comment"].startswith("mixture of regioisomers; trans-3 isomer shown")


def test_number_in_quote_handles_unicode_minus_and_trailing_zeros():
    assert extractor._number_in_quote(-5.2, "a HOMO of −5.20 eV")
    assert extractor._number_in_quote(5.4, "IP = 5.4 eV")
    assert not extractor._number_in_quote(5.4, "IP = 15.45 eV")


def make_pdf(path, text):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)


@pytest.fixture
def paper(tmp_path, monkeypatch, ledger):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    make_pdf(pdfs / "10.1000_x.pdf", "main")
    monkeypatch.setattr(fetch, "get_fulltext", functools.partial(fetch.get_fulltext, pdf_dir=pdfs))
    monkeypatch.setattr(fetch, "si_files", functools.partial(fetch.si_files, pdf_dir=pdfs))
    monkeypatch.setattr(fetch, "get_work", lambda doi: {"title": "A paper", "referenced_works": ["W1"]})
    monkeypatch.setattr(fetch, "reference_list", lambda work: [{"doi": "10.1000/ref", "title": "Ref"}])
    monkeypatch.setattr(extractor, "EXTRACTIONS", tmp_path / "extractions")
    ledger.add("10.1000/x")
    return pdfs


def test_extract_reads_main_only_when_enough(ledger, paper, monkeypatch):
    make_pdf(paper / "10.1000_x_SI1.pdf", "si")
    fake = FakeClaude(answer(compound(), references=[
        {"doi": "10.1000/ref", "likelihood": "likely", "sentence": "as in ref 3"},
        {"doi": "https://doi.org/10.1000/X", "likelihood": "possible", "sentence": "self"}]))
    monkeypatch.setattr(llm, "ask_json", fake)
    summary = extractor.extract(ledger, "10.1000/x")
    assert summary["passes"] == 1 and summary["status"] == DONE and summary["new_references"] == 1
    sent = fake.calls[0]["content"]
    assert [b["type"] for b in sent] == ["document", "text"]
    assert "cache_control" in sent[0]  # SI exists, so a second pass may reuse the main PDF
    assert "10.1000/ref: Ref" in sent[1]["text"] and "not attached" in sent[1]["text"]
    assert ledger.get("10.1000/ref")["status"] == TO_READ
    saved = json.loads((paper.parent / "extractions" / "10.1000_x.json").read_text(encoding="utf-8"))
    assert saved["passes"] == 1


def test_extract_second_pass_with_si(ledger, paper, monkeypatch):
    make_pdf(paper / "10.1000_x_SI1.pdf", "si")
    fake = FakeClaude(answer(needs_si=True, needs_si_reason="CV reference in SI"),
                      answer(compound(), compound(names=["X2"], smiles=None)))
    monkeypatch.setattr(llm, "ask_json", fake)
    summary = extractor.extract(ledger, "10.1000/x")
    assert summary["passes"] == 2 and summary["status"] == PARTIAL
    assert [b["type"] for b in fake.calls[1]["content"]] == ["document", "document", "text"]
    assert fake.calls[1]["task"].endswith(":si")


def test_paper_needing_missing_si_keeps_its_records_and_waits_for_it(ledger, paper, monkeypatch):
    fake = FakeClaude(answer(compound(), needs_si=True, needs_si_reason="DFT energies in SI Table 1"))
    monkeypatch.setattr(llm, "ask_json", fake)
    summary = extractor.extract(ledger, "10.1000/x")
    assert (summary["passes"], summary["status"]) == (1, AWAITING_SI)
    assert "cache_control" not in fake.calls[0]["content"][0]
    assert "We don't have the supporting information" in fake.calls[0]["content"][-1]["text"]
    source = ledger.get("10.1000/x")
    assert (source["status"], source["reason"]) == (AWAITING_SI, "SI needed: DFT energies in SI Table 1")
    assert len(ledger.records("data")) == 1  # what the main text gave is kept
    assert extractor.reprocess(ledger, "10.1000/x") == AWAITING_SI  # rebuilding doesn't lose the wish


def test_review_decision_keeps_a_paper_waiting_for_its_si(ledger, paper, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeClaude(answer(compound(structure_confident=False), needs_si=True)))
    extractor.extract(ledger, "10.1000/x")
    [rec] = ledger.records("review")
    from filips_agent import review
    review.apply_rows(ledger, [{"id": rec["id"], "decision": "accept"}])
    assert ledger.get("10.1000/x")["status"] == AWAITING_SI


def test_paper_read_again_with_its_si_goes_straight_to_the_si_pass(ledger, paper, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeClaude(answer(compound(), needs_si=True, needs_si_reason="SI Table 1")))
    extractor.extract(ledger, "10.1000/x")
    make_pdf(paper / "10.1000_x_SI1.pdf", "si")
    fake = FakeClaude(answer(compound(), compound(names=["X2"], smiles=None)))
    monkeypatch.setattr(llm, "ask_json", fake)
    summary = extractor.extract(ledger, "10.1000/x")
    assert summary["passes"] == 1 and summary["status"] == PARTIAL
    assert [b["type"] for b in fake.calls[0]["content"]] == ["document", "document", "text"]


def test_extract_uncertain_and_not_osc(ledger, paper, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeClaude(answer(compound(), uncertain="IP of 7.9 eV")))
    assert extractor.extract(ledger, "10.1000/x")["status"] == REVIEW
    monkeypatch.setattr(llm, "ask_json", FakeClaude(answer(is_osc=False)))
    assert extractor.extract(ledger, "10.1000/x")["status"] == IRRELEVANT


def test_reprocess_rebuilds_records_from_saved_extraction(ledger, paper, monkeypatch):
    monkeypatch.setattr(llm, "ask_json", FakeClaude(answer(compound())))
    extractor.extract(ledger, "10.1000/x")
    ledger.db.execute("DELETE FROM records")
    assert extractor.reprocess(ledger, "10.1000/x") == DONE
    assert len(ledger.records("data")) == 1
    assert extractor.reprocess(ledger, "10.1000/never-read") is None
