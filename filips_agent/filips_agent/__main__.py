"""Command line, run from the filips_agent/ folder:

    python -m filips_agent add 10.1002/adma.201602776 ...   seed the reading list
    python -m filips_agent info 10.1002/adma.201602776      OpenAlex summary of one paper
    python -m filips_agent fetch [--batch 10]               get full texts, or wish-list them
    python -m filips_agent inbox [--no-llm]                 file PDFs dropped in data/inbox/
    python -m filips_agent extract [--batch 10] [--model M] read papers on the reading list
    python -m filips_agent triage [--batch 50] [--model M]  judge abstracts of "possible" references
    python -m filips_agent run [--batch 10]                 inbox, extract, triage, export
    python -m filips_agent reprocess                        rebuild records from saved extractions (no API)
    python -m filips_agent status                           counts per status
    python -m filips_agent export                           write the CSV tables to data/output/
"""
import argparse
import json
import time

import anthropic

from . import fetch, llm
from .agents import extractor, inbox_sorter, triage
from .ledger import TO_READ, Ledger


def do_inbox(ledger, use_llm=True):
    filed, skipped = fetch.collect_inbox(ledger, sorter=inbox_sorter.sort_file if use_llm else None)
    for doi, paths in filed.items():
        print(f"filed  {doi}: {', '.join(p.name for p in paths)}")
    for name, why in skipped.items():
        print(f"left   {name}: {why}")
    print(f"{len(filed)} paper(s) filed; {len(skipped)} file(s) left in {fetch.INBOX}")


def do_extract(ledger, batch, model=None):
    for source in ledger.queue(TO_READ, batch):
        doi = source["doi"]
        if not fetch.acquire(ledger, doi):
            print(f"wish list  {doi}")
            continue
        try:
            r = extractor.extract(ledger, doi, model)
        except (llm.LLMError, anthropic.APIError) as e:
            print(f"FAILED     {doi}: {e}")
            continue
        print(f"{r['status']:10} {doi}  compounds: {r['compounds']}, passes: {r['passes']}, "
              f"new references: {r['new_references']}, ${r['cost_usd']:.2f}")
    fetch.INBOX.mkdir(parents=True, exist_ok=True)


def do_triage(ledger, batch, model=None):
    for doi, status, reason in triage.run(ledger, batch, model):
        print(f"{status:10} {doi}  {reason[:100]}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="filips_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add").add_argument("dois", nargs="+")
    sub.add_parser("info").add_argument("doi")
    sub.add_parser("fetch").add_argument("--batch", type=int, default=10)
    sub.add_parser("inbox").add_argument("--no-llm", action="store_true", help="rules only, no Haiku check")
    for name, batch in (("extract", 10), ("triage", 50), ("run", 10)):
        s = sub.add_parser(name)
        s.add_argument("--batch", type=int, default=batch)
        s.add_argument("--model", help="Claude model id (default: llm.MODELS)")
    for name in ("status", "export", "reprocess"):
        sub.add_parser(name)
    a = p.parse_args(argv)
    started = time.time()

    with Ledger() as ledger:
        if a.cmd == "add":
            for doi in a.dois:
                new = ledger.add(doi, TO_READ, reason="seed")
                print(f"{'added' if new else 'already known'}: {doi}")
        elif a.cmd == "info":
            work = fetch.get_work(a.doi)
            if not work:
                print("not found in OpenAlex")
                return
            refs = fetch.reference_dois(work)
            print(f"{work['title']}\n{work['journal']} ({work['year']})  open access: {work['is_oa']}")
            print(f"references with DOI: {len(refs)} of {len(work['referenced_works'])}")
            print(f"abstract: {(work['abstract'] or 'not available')[:300]}")
        elif a.cmd == "fetch":
            for source in ledger.queue(TO_READ, a.batch):
                text = fetch.acquire(ledger, source["doi"])
                print(f"{'full text' if text else 'wish list'}: {source['doi']}")
            fetch.INBOX.mkdir(parents=True, exist_ok=True)  # where wished-for PDFs are dropped
        elif a.cmd == "inbox":
            do_inbox(ledger, not a.no_llm)
        elif a.cmd == "extract":
            do_extract(ledger, a.batch, a.model)
        elif a.cmd == "triage":
            do_triage(ledger, a.batch, a.model)
        elif a.cmd == "run":
            do_inbox(ledger)
            do_extract(ledger, a.batch, a.model)
            do_triage(ledger, 50)
            print(f"tables written to {ledger.export()}")
        elif a.cmd == "reprocess":
            for path in sorted(extractor.EXTRACTIONS.glob("*.json")):
                doi = json.loads(path.read_text(encoding="utf-8"))["doi"]
                print(f"{extractor.reprocess(ledger, doi):10} {doi}")
            print(f"tables written to {ledger.export()}")
        elif a.cmd == "status":
            for status, n in sorted(ledger.counts().items()):
                print(f"{status:14} {n}")
        elif a.cmd == "export":
            print(f"tables written to {ledger.export()}")

    spent = llm.total_cost(since=started)
    if spent:
        print(f"Claude API cost this run: ${spent:.2f}")


if __name__ == "__main__":
    main()
