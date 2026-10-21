import csv
from pathlib import Path

from eval.score import score

GOLD = list(csv.DictReader(open(Path(__file__).parents[1] / "eval" / "gold_labels.csv", encoding="utf-8")))


def agent_row(gold, **changes):
    row = {k: gold[k] for k in gold} | {"target": "data", "review_reason": None}
    row["source_doi"] = gold["source_doi"].lower()
    return row | changes


def test_perfect_rows_score_full_marks():
    report = score(GOLD, [agent_row(g) for g in GOLD])
    assert all(r["match"] == "structure" for r in report)
    assert all(all(r["fields"].values()) for r in report)


def test_tolerance_wrong_method_and_wrong_structure():
    g = next(r for r in GOLD if r["name"] == "IO-4Cl")
    rows = [agent_row(g, ip="5.73", ea="3.90", ea_method="CV + Eg")]
    [r] = [x for x in score(GOLD, rows) if x["gold"] is g]
    assert r["fields"]["ip"] and not r["fields"]["ea"] and not r["fields"]["ea_method"]

    wrong = agent_row(g, smiles="c1ccsc1")
    [r] = [x for x in score(GOLD, [wrong]) if x["gold"] is g]
    assert r["match"] == "name only"


def test_missing_paper():
    report = score(GOLD, [])
    assert {r["match"] for r in report} == {"paper not extracted"}
