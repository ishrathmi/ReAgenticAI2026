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


def test_repair_step():
    """A correction is kept only if it removes a flagged problem, adds none and is backed by a verified quote."""
    from litagent.domains.peptide_solubility import REPAIRS, enrich
    from litagent.models import FakeModel
    from litagent.tools.repair import GENERIC, repair
    _, schema = load_profile("peptide_solubility")
    rules = {**GENERIC, **REPAIRS}
    passage = "AA | 0.077±0.003 | 0.053"
    paper = "N-acetylated and C-amidated peptides were synthesized. " + passage

    def flagged(**kw):
        rec = {"peptide_name": "AA (ala-ala)", "solubility_value": 0.077, "evidence": "AA | 0.077±0.003",
               "evidence_verified": True, **kw}
        return enrich(rec)

    # 1. Invented unit: the model removes it -> accepted, flag cleared, change recorded
    rec = flagged(solubility_unit="mg/mL", unit_in_source=False, c_terminus="free")
    assert "not found in source text" in rec["review_reason"]
    fix = FakeModel(replies=['{"record": {"solubility_unit": null}, "repair_evidence": null, "explanation": "no unit in passage"}'])
    out = repair(rec, passage, paper, "", schema, fix, enrich, rules)
    assert out["repair_outcome"] == "accepted" and not out["needs_review"]
    assert out["solubility_unit"] is None and "mg/mL" in out["repaired_fields"]
    assert out["data_caveats"] == "unit not stated by source"

    # 2. The model replaces it with another unit that is also not in the passage -> rejected, original kept
    rec = flagged(solubility_unit="mg/mL", unit_in_source=False, c_terminus="free")
    bad = FakeModel(replies=['{"record": {"solubility_unit": "µM"}, "repair_evidence": "AA | 0.077±0.003"}'])
    out = repair(rec, passage, paper, "", schema, bad, enrich, rules)
    assert out["repair_outcome"] == "rejected" and out["solubility_unit"] == "mg/mL" and out["needs_review"]

    # 3. Amide: accepted only with a quote that exists in the paper
    rec = flagged(solubility_unit="mole fraction", paper_mentions_c_amide=True)
    assert "amidation" in rec["review_reason"]
    no_quote = FakeModel(replies=['{"record": {"c_terminus": "amide"}, "repair_evidence": null}'])
    assert repair(dict(rec), passage, paper, "", schema, no_quote, enrich, rules)["repair_outcome"] == "rejected"
    invented = FakeModel(replies=['{"record": {"c_terminus": "amide"}, "repair_evidence": "all peptides were amidated"}'])
    assert repair(dict(rec), passage, paper, "", schema, invented, enrich, rules)["repair_outcome"] == "rejected"
    good = FakeModel(replies=['{"record": {"c_terminus": "amide"}, "repair_evidence": "C-amidated peptides were synthesized"}'])
    out = repair(dict(rec), passage, paper, "", schema, good, enrich, rules)
    assert out["repair_outcome"] == "accepted" and out["c_terminus"] == "amide" and not out["needs_review"]

    # 4. Fields outside the allowed list are ignored, so the value cannot be changed by a repair
    rec = flagged(solubility_unit="mg/mL", unit_in_source=False, c_terminus="free")
    sneaky = FakeModel(replies=['{"record": {"solubility_unit": null, "solubility_value": 99}}'])
    assert repair(rec, passage, paper, "", schema, sneaky, enrich, rules)["solubility_value"] == 0.077

    # 5. A record with no repairable problem makes no model call
    clean = flagged(solubility_unit="mole fraction", c_terminus="free")
    assert repair(clean, passage, paper, "", schema, FakeModel(replies=[]), enrich, rules) is clean


def test_flag_diagnosis():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("cm", pathlib.Path(__file__).parents[1] / "compare_models.py")
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    gold = [{"source_file": "a.pdf", "peptide_name": "P1", "solubility_value": "1", "solubility_unit": "mg/mL"},
            {"source_file": "a.pdf", "peptide_name": "P2", "solubility_value": "2", "solubility_unit": ""}]
    recs = [{"source_file": "a.pdf", "peptide_name": "P1", "solubility_value": 1, "solubility_unit": "mg/mL",
             "needs_review": True, "review_reason": "low model confidence"},
            {"source_file": "a.pdf", "peptide_name": "P2", "solubility_value": 2, "solubility_unit": "mg/mL",
             "needs_review": True, "review_reason": "unit 'mg/mL' not found in source text"}]
    summary, rules, missed = cm.flag_diagnosis(gold, recs)
    assert summary == {"errors caught": 1, "errors total": 1, "correct records flagged": 1, "correct records total": 1}
    assert rules == {"low model confidence": (1, 0), "unit '…' not found in source text": (1, 1)}
    assert missed == {}


def test_control_characters_from_pdf(tmp_path):
    """PDF control characters: decoded as a minus in units, removed elsewhere, never break the Excel file."""
    from litagent.domains.peptide_solubility import to_mg_per_ml
    from litagent.tools.extract import coerce
    _, schema = load_profile("peptide_solubility")
    rec = coerce({"solubility_unit": "mg ml\x011", "notes": "23 \x01 2 °C"}, schema)
    assert rec["solubility_unit"] == "mg ml-1" and rec["notes"] == "23  2 °C"
    assert to_mg_per_ml(2, rec["solubility_unit"], None)[0] == 2.0
    _, xlsx = output.write([{"peptide_name": "P1", "evidence": "at 23 \x01 2 °C"}], [], schema, str(tmp_path), "db")
    assert load_workbook(xlsx).sheetnames[0] == "Records"          # writing did not raise


def test_sequence_and_solvent_rules():
    from litagent.domains.peptide_solubility import enrich, passage_flags
    caption = "Table 2 The solubility of AA (ala-ala) and GG (gly-gly) in water-DMSO mixture from 0% to 80%."
    assert passage_flags(caption) == {"passage_has_sequence": True, "passage_has_cosolvent_percent": True}
    assert passage_flags("Solubility was measured by HPLC in water.") == {
        "passage_has_sequence": False, "passage_has_cosolvent_percent": False}
    base = {"peptide_name": "P3", "solubility_value": 1, "solubility_unit": "mg/mL", "unit_in_source": True,
            "evidence_verified": True, "c_terminus": "free"}
    # No sequence anywhere in the passage: a gap in the source, not an error
    rec = enrich({**base, "passage_has_sequence": False})
    assert not rec["needs_review"] and "sequence not stated" in rec["data_caveats"]
    # The passage contains a sequence the model did not extract: review
    rec = enrich({**base, "passage_has_sequence": True})
    assert rec["needs_review"] and "sequence is empty" in rec["review_reason"]
    # Mixture without composition, while the passage gives percentages: review
    seq = {**base, "peptide_name": "AA (ala-ala)"}
    rec = enrich({**seq, "solvent": "water-DMSO mixture", "passage_has_cosolvent_percent": True})
    assert rec["needs_review"] and "composition is missing" in rec["review_reason"]
    for solvent in ("water/DMSO, 20% DMSO by mass", "water", "DMSO"):
        assert not enrich({**seq, "solvent": solvent, "passage_has_cosolvent_percent": True})["needs_review"]


def test_solvent_scoring_rejects_added_cosolvent():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("cm", pathlib.Path(__file__).parents[1] / "compare_models.py")
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    gold = {"solvent": "water"}
    assert cm.field_checks(gold, {"solvent": "water"})["solvent"]
    assert not cm.field_checks(gold, {"solvent": "water-DMSO mixture"})["solvent"]
    assert cm.field_checks({"solvent": "water/DMSO, 20% DMSO by mass"},
                           {"solvent": "water/DMSO, 20% DMSO by mass"})["solvent"]
