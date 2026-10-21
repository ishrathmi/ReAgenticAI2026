"""Image check of structures, with fake DECIMER/MolScribe workers (no models needed)."""
import json

import pymupdf
import pytest

from filips_agent import fetch, ocsr, resolve
from filips_agent.ledger import Ledger
from filips_agent.normalize_values import normalize_record

THIOPHENE_C6 = "CCCCCCc1ccsc1"


def test_compare_levels():
    assert ocsr.compare(THIOPHENE_C6, "CCCCCCC1=CSC=C1") == "exact"
    assert ocsr.compare("CCc1ccsc1", THIOPHENE_C6) == "core"          # side chain drawn differently
    assert ocsr.compare("*c1ccsc1", THIOPHENE_C6) == "core"           # side chain drawn as R
    assert ocsr.compare("c1ccoc1", THIOPHENE_C6) == "different"
    assert ocsr.compare(None, THIOPHENE_C6) is None


@pytest.mark.parametrize("readings, compound, verdict", [
    ({"decimer": THIOPHENE_C6, "molscribe": THIOPHENE_C6}, {"smiles": THIOPHENE_C6}, "matches"),
    ({"decimer": "*c1ccsc1"}, {"smiles": THIOPHENE_C6}, "core matches (side chains differ)"),
    ({"decimer": "c1ccoc1"}, {"smiles": THIOPHENE_C6}, "different compound?"),
    ({"decimer": "c1ccoc1"}, {"smiles": THIOPHENE_C6, "reported_formula": "C4H4O"}, "agent's structure may be wrong"),
    ({"decimer": THIOPHENE_C6}, {"smiles": None, "reported_formula": "C10H16S"}, "candidate structure"),
    ({"decimer": THIOPHENE_C6}, {"smiles": None}, "unconfirmed reading"),
    ({"decimer": "not smiles"}, {"smiles": THIOPHENE_C6}, "unreadable"),
])
def test_verdicts(readings, compound, verdict):
    j = ocsr.judge({"smiles": readings}, {"names": ["X"], "reported_formula": None} | compound)
    assert j["verdict"] == verdict


def test_tools_agree_flag():
    j = ocsr.judge({"smiles": {"decimer": THIOPHENE_C6, "molscribe": "CCCCCCC1=CSC=C1"}},
                   {"names": ["X"], "smiles": THIOPHENE_C6, "reported_formula": None})
    assert j["tools_agree"]


@pytest.fixture
def paper(tmp_path, monkeypatch):
    """A one-page paper with a structure figure showing two labelled compounds, and its extraction."""
    pdfs, data = tmp_path / "pdfs", tmp_path / "data"
    pdfs.mkdir()
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(60, 60, 400, 200), color=(0, 0, 0))
    page.insert_text((80, 215), "T-6")
    page.insert_text((250, 215), "F-6")
    page.insert_text((60, 240), "Figure 1. Chemical structures of T-6 and F-6.")
    doc.save(pdfs / "10.1000_x.pdf")
    (data / "extractions").mkdir(parents=True)
    result = {"compounds": [{"names": ["T-6"], "label_in_paper": None, "smiles": THIOPHENE_C6, "reported_formula": None}]}
    (data / "extractions" / "10.1000_x.json").write_text(json.dumps({"doi": "10.1000/x", "result": result}))
    monkeypatch.setattr(ocsr, "DATA_DIR", data)
    monkeypatch.setattr(ocsr, "CACHE_DIR", data / "cache")
    monkeypatch.setattr(fetch, "get_fulltext", lambda doi: {"main": pdfs / "10.1000_x.pdf", "si": []})
    monkeypatch.setattr(ocsr, "available", lambda: {"decimer": True, "molscribe": True})
    calls = []

    def worker(tool, job):
        calls.append(tool)
        if tool == "decimer":  # two drawings side by side, pixel boxes at 300 dpi; labels only in the text layer
            return [{"page": 0, "bbox": [300, 300, 900, 800], "image": "a.png", "ocr": [], "decimer": THIOPHENE_C6},
                    {"page": 0, "bbox": [1000, 300, 1600, 800], "image": "b.png", "ocr": [], "decimer": "CCCCCCc1ccoc1"}]
        return [{"image": "a.png", "molscribe": "CCCCCCC1=CSC=C1"}, {"image": "b.png", "molscribe": None}]

    monkeypatch.setattr(ocsr, "_run_worker", worker)
    return calls


def test_check_paper_picks_the_labelled_drawing_and_caches(paper):
    [row] = ocsr.check_paper("10.1000/x")
    assert (row["compound"], row["picked_by"], row["image"]) == ("T-6", "label", "a.png")
    assert (row["verdict"], row["tools_agree"], row["figure"]) == ("matches", "yes", "Figure 1 (p. 1)")
    assert paper == ["decimer", "molscribe"]
    ocsr.check_paper("10.1000/x")
    assert paper == ["decimer", "molscribe"]  # second time from the cache


def test_check_paper_only_checks_the_named_compounds(paper):
    assert ocsr.check_paper("10.1000/x", only=["F-6"]) == []
    assert [r["compound"] for r in ocsr.check_paper("10.1000/x", only=["T-6, thiophene"])] == ["T-6"]


def record(name):
    rows, _ = normalize_record({"names": [name], "smiles": THIOPHENE_C6, "source_doi": "10.1000/x",
                                "values": [{"label": "IP", "value": 5.4, "origin": "measured", "technique": "UPS"}]})
    return rows


def test_only_records_in_review_for_a_structure_reason_are_flagged(tmp_path):
    with Ledger(tmp_path / "l.db") as lg:
        lg.add("10.1000/x")
        lg.save_compounds("10.1000/x", [(record("T-6"), ["structure uncertain (P3c): mixture"]),
                                        (record("F-6"), ["IP: outside the usual range"]),
                                        (record("D-6"), [])])
        assert ocsr.flagged_structures(lg) == {"10.1000/x": ["T-6"]}
        assert ocsr.flagged_structures(lg, "10.1000/other") == {}


def test_check_flagged_notes_the_result_once_after_extraction(paper, tmp_path):
    with Ledger(tmp_path / "l.db") as lg:
        lg.add("10.1000/x")
        lg.save_compounds("10.1000/x", [(record("T-6"), ["structure uncertain (P3c): mixture"])])
        [row] = ocsr.check_flagged(lg, "10.1000/x")
        assert row["compound"] == "T-6"
        [rec] = lg.records("review")
        assert rec["review_reason"].endswith("; image check: matches (Figure 1 (p. 1), label)")
        assert ocsr.flagged_structures(lg, unchecked=True) == {}  # `images` won't redo it


def test_check_flagged_needs_tools_only_when_something_is_flagged(paper, tmp_path, monkeypatch):
    monkeypatch.setattr(ocsr, "available", lambda: {"decimer": False, "molscribe": False})
    with Ledger(tmp_path / "l.db") as lg:
        lg.add("10.1000/x")
        lg.save_compounds("10.1000/x", [(record("T-6"), [])])
        assert ocsr.check_flagged(lg, "10.1000/x") == []
        lg.save_compounds("10.1000/x", [(record("T-6"), ["no structure"])])
        with pytest.raises(ocsr.ToolsMissing):
            ocsr.check_flagged(lg, "10.1000/x")


def test_result_goes_into_the_review_reason(tmp_path):
    with Ledger(tmp_path / "l.db") as lg:
        lg.add("10.1000/x")
        rows, _ = normalize_record({"names": ["T-6"], "smiles": THIOPHENE_C6, "source_doi": "10.1000/x",
                                    "values": [{"label": "IP", "value": 5.4, "origin": "measured", "technique": "UPS"}]})
        lg.save_compounds("10.1000/x", [(rows, ["W2b: check"])])
        report = [{"doi": "10.1000/x", "compound": "T-6", "verdict": "matches", "figure": "Figure 1 (p. 1)",
                   "picked_by": "label"}]
        ocsr.note_in_review(lg, report)
        ocsr.note_in_review(lg, report)  # not added twice
        [rec] = lg.records("review")
        assert rec["review_reason"] == "W2b: check; image check: matches (Figure 1 (p. 1), label)"
