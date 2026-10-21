"""Compare triage models on the same abstracts, so a person can pick one.

Run from the energy_extractor/ folder:  .venv/Scripts/python -m eval.triage_trial [--n 30]

Takes papers cited by the gold papers that have an abstract in OpenAlex, runs abstract
triage (rules A1-A4) with each model, and writes data/output/triage_trial.csv with every
model's verdict and reason side by side. The ledger is not touched.
"""
import argparse
import csv
import random
from pathlib import Path

from energy_extractor import fetch, llm
from energy_extractor.agents import triage
from energy_extractor.ledger import DATA_DIR

GOLD = Path(__file__).with_name("gold_labels.csv")
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


def sample_abstracts(n, seed=0):
    gold = [r["source_doi"] for r in csv.DictReader(open(GOLD, encoding="utf-8"))]
    refs = []
    for doi in gold:
        work = fetch.get_work(doi)
        refs += fetch.reference_dois(work) if work else []
    works = fetch.get_works(sorted(set(refs) - {d.lower() for d in gold}))
    with_abstract = sorted((w for w in works.values() if w["abstract"]), key=lambda w: w["doi"])
    return random.Random(seed).sample(with_abstract, min(n, len(with_abstract)))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--models", nargs="+", default=MODELS)
    a = p.parse_args(argv)

    papers = sample_abstracts(a.n)
    print(f"{len(papers)} abstracts, models: {', '.join(a.models)}")
    cost = {m: 0.0 for m in a.models}
    rows = []
    for i, w in enumerate(papers, 1):
        row = {"doi": w["doi"], "title": w["title"]}
        text = f"Title: {w['title']}\n\nAbstract: {w['abstract']}"
        for m in a.models:
            answers, usage = llm.ask_json(m, triage.SYSTEM, [{"type": "text", "text": text}], triage.SCHEMA,
                                          effort="low", max_tokens=8000, task=f"triage-trial:{m}")
            status, _ = triage.verdict(answers)
            short = m.removeprefix("claude-").split("-")[0]
            row[f"{short}_verdict"] = "read" if status == "to_read" else "irrelevant"
            row[f"{short}_reason"] = answers["reason"]
            cost[m] += usage["cost_usd"]
        verdicts = {row[k] for k in row if k.endswith("_verdict")}
        row["models_agree"] = "yes" if len(verdicts) == 1 else "NO"
        rows.append(row)
        print(f"{i:3}. {row['models_agree']:3} " + "  ".join(f"{k.split('_')[0]}={row[k]}" for k in row
                                                            if k.endswith("_verdict")) + f"  {w['title'][:60]}")

    out = DATA_DIR / "output" / "triage_trial.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, rows[0].keys(), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    agree = sum(r["models_agree"] == "yes" for r in rows)
    print(f"\nAll models agree on {agree}/{len(rows)}. Written to {out}")
    for m, c in cost.items():
        print(f"  {m:20} ${c:.4f} total, ${c / len(rows):.5f} per abstract")


if __name__ == "__main__":
    main()
