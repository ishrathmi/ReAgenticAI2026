"""Command line, run from the filips_agent/ folder:

    python -m filips_agent add 10.1002/adma.201602776 ...   seed the reading list
    python -m filips_agent info 10.1002/adma.201602776      OpenAlex summary of one paper
    python -m filips_agent fetch [--batch 10]               get full texts, or wish-list them
    python -m filips_agent inbox [--no-llm]                 file PDFs dropped in data/inbox/
    python -m filips_agent extract [--batch 10] [--model M] read papers on the reading list
    python -m filips_agent triage [--batch 50] [--model M]  judge abstracts of "possible" references
    python -m filips_agent run [--batch 10]                 inbox, extract, triage, export
    python -m filips_agent review                           apply decisions typed into review.csv
    python -m filips_agent reprocess                        rebuild records from saved extractions (no API)
    python -m filips_agent images [--doi D] [--si] [--all]  check structures in review against the paper's drawings
    python -m filips_agent web [--port 8000]                browser page at http://127.0.0.1:8000
    python -m filips_agent status                           counts per status
    python -m filips_agent export                           write the CSV tables to data/output/
"""
import argparse
import json
import time

from . import fetch, llm
from .agents import extractor
from .ledger import TO_READ, Ledger
from .pipeline import apply_and_export, do_extract, do_images, do_inbox, do_triage


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
    for name in ("status", "export", "reprocess", "review"):
        sub.add_parser(name)
    s = sub.add_parser("images", help="check structures against the paper's drawings (DECIMER, MolScribe)")
    s.add_argument("--doi", help="one paper (default: every paper read so far)")
    s.add_argument("--si", action="store_true", help="also look in the SI PDFs")
    s.add_argument("--refresh", action="store_true", help="read the drawings again instead of using the cache")
    s.add_argument("--all", action="store_true", dest="everything",
                   help="every compound, not only those in review for a structure reason")
    s = sub.add_parser("web", help="browser page: drop papers, see the tables, review")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    a = p.parse_args(argv)
    started = time.time()
    if a.cmd == "web":
        from . import web  # Flask is only needed for the page
        web.serve(a.port, not a.no_browser)
        return

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
            apply_and_export(ledger)
        elif a.cmd == "reprocess":
            for path in sorted(extractor.EXTRACTIONS.glob("*.json")):
                doi = json.loads(path.read_text(encoding="utf-8"))["doi"]
                print(f"{extractor.reprocess(ledger, doi):10} {doi}")
            apply_and_export(ledger)
        elif a.cmd == "status":
            for status, n in sorted(ledger.counts().items()):
                print(f"{status:14} {n}")
        elif a.cmd in ("export", "review"):
            apply_and_export(ledger)
        elif a.cmd == "images":
            do_images(ledger, a.doi, a.si, a.refresh, a.everything)

    spent = llm.total_cost(since=started)
    if spent:
        print(f"Claude API cost this run: ${spent:.2f}")


if __name__ == "__main__":
    main()
