#!/usr/bin/env python3
"""Fetch every bioRxiv (or medRxiv) preprint posted in the last N days.

Uses the public API: https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}
Writes a JSON list of papers (latest version per DOI) to --out.
"""
import argparse, json, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

API = "https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}"


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # network hiccups are common on this API
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def fetch(server, start, end, workers=6, progress=None):
    """Kept for older callers; see sources.fetch."""
    from sources import fetch as fetch_source
    papers = fetch_source(server, start, end, progress=progress)
    return {"server": server, "start": start.isoformat(), "end": end.isoformat(), "papers": papers}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--server", default="biorxiv", choices=["biorxiv", "medrxiv", "chemrxiv"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    end = date.fromisoformat(a.end)
    raw = fetch(a.server, end - timedelta(days=a.days), end, a.workers)
    with open(a.out, "w") as f:
        json.dump(raw, f)
    print(f"saved {len(raw['papers'])} unique papers -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
