"""The pipeline steps, shared by the command line (__main__.py) and the browser page (web.py).

Each step prints what it did; the page shows that output as the job log.
"""
import json

import anthropic

from . import fetch, llm, ocsr, review
from .agents import extractor, inbox_sorter, triage
from .ledger import TO_READ, WISHED


def do_inbox(ledger, use_llm=True, inbox=None):
    """File what was dropped in the inbox.

    A paper the ledger already knows goes back on the reading list when its PDF wasn't filed
    yet; when it was, the copy is left in the inbox with the paper's status.
    Returns (filed, skipped): {doi: [paths]} and {file name: why it was left there}.
    """
    inbox = inbox or fetch.INBOX
    leave = {}
    for name, (doi, added) in fetch.identify_dropped(ledger, inbox).items():
        source = ledger.get(doi)
        if added:
            print(f"new    {doi}: {name} (added to the reading list)")
        elif source["status"] in (TO_READ, *WISHED):
            pass  # wanted: collect_inbox files it
        elif fetch.get_fulltext(doi):
            leave[name] = (f"already have this paper: {doi} was read before ({source['status']}: "
                           f"{source['reason'] or 'no reason noted'}); remove the copy")
        else:
            ledger.set_status(doi, TO_READ, "PDF dropped in")
            print(f"again  {doi}: {name} (was {source['status']}; back on the reading list)")
    filed, skipped = fetch.collect_inbox(ledger, inbox, sorter=inbox_sorter.sort_file if use_llm else None,
                                         leave=leave)
    for doi, paths in filed.items():
        print(f"filed  {doi}: {', '.join(p.name for p in paths)}")
    for name, why in skipped.items():
        print(f"left   {name}: {why}")
    print(f"{len(filed)} paper(s) filed; {len(skipped)} file(s) left in {inbox}")
    return filed, skipped


def do_extract(ledger, batch, model=None):
    checked = []  # image checks of structures sent to review
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
        try:
            rows = ocsr.check_flagged(ledger, doi)
        except ocsr.ToolsMissing as e:
            print(f"           {e}")
            continue
        checked += rows
        for row in rows:
            print(f"           image check: {row['verdict']} for {row['compound']} ({row['figure']}, {row['picked_by']})")
    if checked:
        print(f"image check report written to {ocsr.write_report(checked)}")
    fetch.INBOX.mkdir(parents=True, exist_ok=True)


def apply_and_export(ledger):
    """Apply decisions typed into review.csv first, so rewriting the file never loses them."""
    try:
        for message in review.apply(ledger):
            print(f"review  {message}")
        print(f"tables written to {ledger.export()}")
    except PermissionError as e:
        print(f"could not open {e.filename}; close it (e.g. in Excel) and run again")


def do_images(ledger, doi=None, si=False, refresh=False, everything=False):
    tools = ocsr.available()
    if not tools["decimer"]:
        print("the image tools aren't installed; see ocsr/README.md")
        return
    if not tools["molscribe"]:
        print("MolScribe isn't installed: DECIMER only")
    if everything:
        dois = [doi] if doi else [json.loads(p.read_text(encoding="utf-8"))["doi"]
                                  for p in sorted(extractor.EXTRACTIONS.glob("*.json"))]
        targets = dict.fromkeys(dois)
    else:
        targets = ocsr.flagged_structures(ledger, doi, unchecked=not refresh)
        if not targets:
            print("no unchecked records in review for a structure reason "
                  "(--refresh to check them again, --all for every compound)")
            return
    rows = []
    for d, only in targets.items():
        found = ocsr.check_paper(d, si, refresh, only)
        rows += found
        for r in found:
            print(f"{r['verdict']:36} {r['compound'][:24]:24} {r['figure']}, {r['picked_by']}  ({d})")
        if not found:
            print(f"{'no structure drawing found':36} {'':24} ({d})")
    ocsr.note_in_review(ledger, rows)
    print(f"report written to {ocsr.write_report(rows)}")
    apply_and_export(ledger)


def do_triage(ledger, batch, model=None):
    for doi, status, reason in triage.run(ledger, batch, model):
        print(f"{status:10} {doi}  {reason[:100]}")
