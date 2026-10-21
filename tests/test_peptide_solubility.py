"""Offline tests for the peptide solubility profile. No API key needed: python -m pytest"""

import json

import fitz
import pytest
from openpyxl import Workbook, load_workbook

from litagent import agent, output
from litagent.config import load_profile
from litagent.domains.peptide_solubility import molecular_weight, to_mg_per_ml, to_one_letter
from litagent.tools.extract import parse_json, verify_evidence

MAIN = """Sequence Effects on the Aqueous Solubility of Capped Peptides
https://doi.org/10.1234/test.2026.001

Results
Peptide P1 (Ac-KLVFFAE-NH2) was soluble up to 2.5 mg/mL in PBS (pH 7.4) at 25 °C, as determined by
UV absorbance of the supernatant after centrifugation.
Variant 7 was poorly soluble: its solubility in water was below 50 µM by HPLC of the supernatant at 298.15 K."""

REPLY = {"records": [
    {"peptide_name": "P1", "sequence": "Ac-KLVFFAE-NH2", "solubility_value": "2.5", "solubility_operator": "=",
     "solubility_unit": "mg/mL", "method": "UV absorbance", "solvent": "PBS", "ph": "7.4", "temperature_c": 25,
     "evidence": "was soluble up to 2.5 mg/mL in PBS (pH 7.4) at 25 °C", "confidence": 0.9},
    {"peptide_name": "Variant 7", "sequence": None, "solubility_value": 50, "solubility_operator": "<",
     "solubility_unit": "µM", "method": "HPLC", "solvent": "water", "temperature_c": 298.15,
     "evidence": "its solubility in water was below 50 µM", "confidence": 0.8},
    {"peptide_name": "P9", "solubility_value": 10, "solubility_unit": "mg/mL",
     "evidence": "P9 dissolved at 10 mg/mL", "confidence": 0.4},
]}


class RouterModel:
    """Returns REPLY for the chunk containing P1, nothing for anything else."""
    model = "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return json.dumps(REPLY) if "Peptide P1" in messages[-1]["content"] else '{"records": []}'


def test_chemistry():
    assert to_one_letter("Ac-Lys-Leu-Val-NH2") == "KLV"
    assert to_one_letter("Ac-KLVFFAE-NH2") == "KLVFFAE"
    assert to_one_letter("KLV[Aib]F") is None
    assert 893 < molecular_weight("KLVFFAE", "acetyl", "amide") < 895     # Ac-KLVFFAE-NH2 ≈ 894.1
    assert to_mg_per_ml(500, "µg/mL", None)[0] == 0.5
    assert to_mg_per_ml(50, "µM", None)[0] is None
    assert to_mg_per_ml(1, "mM", 1000.0)[0] == pytest.approx(1.0)
    assert to_mg_per_ml(0.07, "mole fraction", None)[0] is None


def test_evidence_and_salvage():
    assert verify_evidence("soluble up to 2.5 mg/mL", "P1 was soluble  up to 2.5 mg/mL in PBS")[0]
    assert not verify_evidence("dissolved at 10 mg/mL", "P1 was soluble up to 2.5 mg/mL")[0]
    cut = '{"records": [{"peptide_name": "A", "solubility_value": 1}, {"peptide_name": "B", "solub'
    assert [r["peptide_name"] for r in parse_json(cut)] == ["A"]


def test_end_to_end(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 550, 800), MAIN, fontsize=10, fontname="helv")
    doc.save(papers / "main_paper_x123.pdf")

    wb = Workbook()                                      # SI table: compound -> sequence
    wb.active.append(["Compound", "Sequence", "Mass"])
    wb.active.append([7, "Ac-LVFFA-NH2", 650.8])
    wb.save(papers / "si_x123.xlsx")

    cfg, schema = load_profile("peptide_solubility")
    cfg = {**cfg, "local_folders": [str(papers)], "queries": [],
           "paper_groups": {"test_paper": ["x123"]}}
    model = RouterModel()
    records, files = agent.run(cfg, schema, model, root=tmp_path)

    by_name = {r["peptide_name"]: r for r in records}
    assert set(by_name) == {"P1", "Variant 7", "P9"}
    p1, v7, p9 = by_name["P1"], by_name["Variant 7"], by_name["P9"]
    assert p1["ph"] == 7.4 and p1["solubility_mg_per_ml"] == 2.5 and not p1["needs_review"]
    assert p1["doi"] == "10.1234/test.2026.001"
    assert v7["sequence"] == "Ac-LVFFA-NH2" and "linked by label" in v7["sequence_source"]
    assert v7["temperature_c"] == pytest.approx(25.0)
    assert v7["solubility_mg_per_ml"] == pytest.approx(50e-6 * v7["calc_mw_g_mol"], rel=1e-3)
    assert p9["needs_review"] and "evidence" in p9["review_reason"]

    csv_path, xlsx_path = output.write(records, files, schema, str(tmp_path / "out"), "db")
    assert csv_path.read_text(encoding="utf-8-sig").startswith("record_id,peptide_name")
    assert load_workbook(xlsx_path).sheetnames == ["Records", "Files processed", "Field guide"]


def test_run_logger(tmp_path):
    from litagent.models import CachedModel, FakeModel
    from litagent.runlog import RunLogger

    log = RunLogger(str(tmp_path / "runs"), "peptide_solubility", "fake")
    model = CachedModel(FakeModel(replies=['{"records": []}']), str(tmp_path / "cache"), logger=log)
    msg = [{"role": "user", "content": "hi"}]
    log.context = {"file": "a.pdf", "location": "p1 text 1"}
    with log.stage("extract"):
        model.chat(msg)
    model.chat(msg)                                   # second call is served from the cache
    s = log.finish(files=1, records=0)
    assert s["llm_calls"] == 1 and s["cache_hits"] == 1
    assert s["wall_time_s"] >= s["inference_time_s"] >= 0
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["run_start", "llm_call", "stage", "llm_call", "run_end"]
    assert events[1]["file"] == "a.pdf"
    assert (tmp_path / "runs" / "runs.csv").read_text().startswith("run_id,")


def test_three_letter_and_names():
    from litagent.domains.peptide_solubility import enrich, sequence_from_name
    assert to_one_letter("ala-ala") == "AA"
    assert to_one_letter("Gly-Ala") == "GA"
    assert to_one_letter("H-GLY-GLY-OH") == "GG"
    assert to_one_letter("AA (ala-ala)") == "AA"
    assert sequence_from_name("AG (ala-gly)") == "ala-gly"
    assert sequence_from_name("P3") is None
    rec = enrich({"peptide_name": "GA (gly-ala)", "solubility_value": 0.1, "solubility_unit": "mole fraction",
                  "evidence_verified": True})
    assert rec["sequence_one_letter"] == "GA" and rec["sequence_source"] == "parsed from peptide name"
    assert not rec["needs_review"]


def test_table_split_and_coverage():
    from litagent.agent import table_coverage
    from litagent.tools.parse_pdf import _split_rows
    table = "c | AA | xcal | GG\n" + "\n".join(f"{i}% | 0.0{i}1 | 0.9{i} | 0.0{i}2" for i in range(9))
    parts = _split_rows(table, 5000, 4)
    assert len(parts) == 3 and all(p.startswith("c | AA | xcal | GG") for p in parts)
    cfg = {"value_field": "solubility_value", "exclude_columns": "cal"}
    recs = [{"solubility_value": 0.001}, {"solubility_value": 0.002}]
    cov = table_coverage(parts[0], recs, cfg)        # rows 0-3: 8 experimental cells, xcal ignored
    assert cov == {"cells": 8, "matched": 2, "coverage": 0.25}


def test_compare_matching():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("cm", pathlib.Path(__file__).parents[1] / "compare_models.py")
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    gold = [{"source_file": "a.pdf", "peptide_name": "teixobactin", "solubility_qualitative": "gel"},
            {"source_file": "a.pdf", "peptide_name": "Arg10-teixobactin", "solubility_qualitative": "gel"},
            {"source_file": "a.pdf", "peptide_name": "Variant 3", "solubility_value": "0.09"}]
    recs = [{"source_file": "a.pdf", "peptide_name": "Arg10–teixobactin", "solubility_qualitative": "gel"},
            {"source_file": "a.pdf", "peptide_name": "GLP-1 variant 3 (Run1)", "solubility_value": 0.09}]
    pairs = cm.match(gold, recs)
    assert pairs[0][1] is None                                  # plain 'teixobactin' must not take Arg10's record
    assert pairs[1][1] is recs[0] and pairs[2][1] is recs[1]


def test_review_flags_and_caveats():
    """needs_review marks likely extraction errors; data_caveats marks gaps in the source itself."""
    from litagent.domains.peptide_solubility import enrich, paper_flags
    from litagent.tools.extract import unit_in_source

    assert unit_in_source("mg/mL", "soluble at 2.5 mg mL⁻¹ in water") is True
    assert unit_in_source("µM", "below 50 μM by HPLC") is True
    assert unit_in_source("mg/mL", "AA | 0.077 | 0.053") is False      # unit invented by the model
    assert unit_in_source(None, "anything") is None

    base = {"peptide_name": "GA (gly-ala)", "solubility_value": 0.1, "evidence_verified": True,
            "c_terminus": "free"}
    # Source gives no unit: a caveat, not a review flag
    rec = enrich({**base, "evidence": "GA 0.115"})
    assert not rec["needs_review"] and rec["data_caveats"] == "unit not stated by source"
    # Unit present in the evidence but not extracted: review
    rec = enrich({**base, "evidence": "GA dissolved at 0.1 mg/mL"})
    assert rec["needs_review"] and "unit is empty" in rec["review_reason"]
    # Unit extracted but absent from the source passage: review
    rec = enrich({**base, "solubility_unit": "mg/mL", "unit_in_source": False, "evidence": "GA 0.115"})
    assert rec["needs_review"] and "not found in source text" in rec["review_reason"]

    # C-terminal amide stated in the paper but not extracted: review
    assert paper_flags("N-acetylated and C-amidated peptides were synthesized")["paper_mentions_c_amide"]
    assert not paper_flags("a native amide bond is replaced with an ester")["paper_mentions_c_amide"]
    no_term = {k: v for k, v in base.items() if k != "c_terminus"}
    rec = enrich({**no_term, "solubility_unit": "mole fraction", "paper_mentions_c_amide": True})
    assert rec["needs_review"] and "amidation" in rec["review_reason"]
    rec = enrich({**no_term, "solubility_unit": "mole fraction", "paper_mentions_c_amide": False})
    assert not rec["needs_review"]
