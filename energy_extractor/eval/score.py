"""Score the agent's stored results against eval/gold_labels.csv.

Run from the energy_extractor/ folder:  .venv/Scripts/python -m eval.score

For each gold row, the agent's rows for the same paper are matched by structure
(InChIKey for small molecules, canonical CXSMILES for polymers), falling back to a shared
name so a wrong structure shows up as such. Energies count as correct within 0.02 eV;
methods must match exactly. Rows in both the data and review tables are scored.
"""
import argparse
import csv
from pathlib import Path

from rdkit import Chem, RDLogger

from energy_extractor.dois import clean_doi
from energy_extractor.ledger import Ledger
from energy_extractor.structures import canonicalize, is_polymer

RDLogger.DisableLog("rdApp.*")
GOLD = Path(__file__).with_name("gold_labels.csv")
ENERGIES = ("homo_dft", "lumo_dft", "ip", "ea")
METHODS = ("dft_method", "ip_method", "ea_method")
TOLERANCE = 0.02


def structure_key(smiles):
    canon, err = canonicalize(smiles)
    if err:
        return None
    if is_polymer(canon):
        return canon
    mol = Chem.MolFromSmiles(canon)
    return Chem.MolToInchiKey(mol) or canon


def names(row):
    return {n.strip().lower() for n in (row.get("name") or "").split(",") if n.strip() and n.strip() != "n/a"}


def _same_method(a, b):
    return a.replace(" ", "").lower() == b.replace(" ", "").lower()


def compare(gold, got):
    """{field: True/False} for the fields gold has a value for (or that either side reports)."""
    result = {}
    for f in ENERGIES:
        if gold[f] == "n/a" and got[f] == "n/a":
            continue
        try:
            result[f] = abs(float(gold[f]) - float(got[f])) <= TOLERANCE
        except ValueError:
            result[f] = False
    for f in METHODS:
        if gold[f] == "n/a" and got[f] == "n/a":
            continue
        result[f] = _same_method(gold[f], got[f])
    return result


def score(gold_rows, agent_rows):
    report = []
    for g in gold_rows:
        doi = clean_doi(g["source_doi"])
        mine = [r for r in agent_rows if r["source_doi"] == doi]
        key = structure_key(g["smiles"])
        by_structure = [r for r in mine if r["smiles"] != "n/a" and structure_key(r["smiles"]) == key]
        by_name = [r for r in mine if names(r) & names(g)]
        candidates, match = (by_structure, "structure") if by_structure else (by_name, "name only")
        if not mine:
            match = "paper not extracted"
        elif not candidates:
            match = "compound not found"
        best = max(candidates, key=lambda r: sum(compare(g, r).values()), default=None)
        fields = compare(g, best) if best else {}
        report.append({"gold": g, "match": match if best else match, "row": best, "fields": fields,
                       "extra": [r for r in mine if r is not best]})
    return report


def print_report(report):
    totals = {"structure": 0, "fields": 0, "correct": 0}
    for r in report:
        g = r["gold"]
        ok = sum(r["fields"].values())
        totals["structure"] += r["match"] == "structure"
        totals["fields"] += len(r["fields"])
        totals["correct"] += ok
        print(f"\n{g['name']}  ({g['source_doi']})  match: {r['match']}"
              + (f", table: {r['row']['target']}" if r["row"] else ""))
        for f, good in r["fields"].items():
            mark = "ok " if good else "XX "
            print(f"   {mark}{f:10} gold {g[f]:<22} agent {r['row'][f]}")
        if r["row"] and r["row"].get("review_reason"):
            print(f"   review reason: {r['row']['review_reason']}")
        for e in r["extra"]:
            print(f"   + also extracted: {e['name']}  ip {e['ip']} ({e['ip_method']}), ea {e['ea']} ({e['ea_method']}), "
                  f"homo_dft {e['homo_dft']}, lumo_dft {e['lumo_dft']}")
    n = len(report)
    print(f"\nStructures matched: {totals['structure']}/{n}   "
          f"Fields correct: {totals['correct']}/{totals['fields']}")
    return totals


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--gold", default=GOLD)
    a = p.parse_args(argv)
    gold_rows = list(csv.DictReader(open(a.gold, encoding="utf-8")))
    with Ledger() as ledger:
        agent_rows = ledger.records("data") + ledger.records("review")
    print_report(score(gold_rows, agent_rows))


if __name__ == "__main__":
    main()
