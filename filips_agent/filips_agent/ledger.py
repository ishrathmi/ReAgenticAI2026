"""SQLite ledger of every source the agent has seen (RULES.md W1, W3, W6, W7).

A DOI is added once and never evaluated again. Statuses:

    to_triage     "possible" reference; its abstract still has to be judged (W1)
    to_read       on the reading list
    awaiting_pdf  relevant, but no full text yet; listed in the wish list (W6)
    done          every compound record went to the data table (W3)
    partial       some records in data, some in review
    review        every record in review, incl. papers marked uncertain (P7c)
    irrelevant    rejected at the abstract or paper stage
"""
import csv
import sqlite3
import time
from pathlib import Path

from .dois import clean_doi
from .normalize_values import COLUMNS

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

TO_TRIAGE, TO_READ, AWAITING_PDF = "to_triage", "to_read", "awaiting_pdf"
DONE, PARTIAL, REVIEW, IRRELEVANT = "done", "partial", "review", "irrelevant"
STATUSES = (TO_TRIAGE, TO_READ, AWAITING_PDF, DONE, PARTIAL, REVIEW, IRRELEVANT)

_RECORD_COLS = [c for c in COLUMNS if c != "source_doi"]
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sources (
    doi TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL,
    likelihood TEXT,             -- seed (NULL), likely or possible
    found_in TEXT,               -- DOI of the paper that cited it
    reason TEXT,
    added_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    source_doi TEXT NOT NULL REFERENCES sources(doi),
    target TEXT NOT NULL CHECK (target IN ('data', 'review')),
    review_reason TEXT,
    {", ".join(f"{c} TEXT" for c in _RECORD_COLS)}
);
"""

# seeds first, then likely, then possible references promoted by triage
_PRIORITY = "CASE likelihood WHEN 'likely' THEN 1 WHEN 'possible' THEN 2 ELSE 0 END"


class Ledger:
    def __init__(self, path=DATA_DIR / "ledger.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- sources ------------------------------------------------------------

    def add(self, doi, status=TO_READ, likelihood=None, found_in=None, title=None, reason=None):
        """Add a source unless it is already known (W7). Returns True if it was new."""
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        now = time.time()
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (clean_doi(doi), title, status, likelihood,
                 clean_doi(found_in) if found_in else None, reason, now, now))
        return cur.rowcount == 1

    def add_reference(self, doi, likelihood, found_in, title=None):
        """W1: likely references go on the reading list, possible ones to abstract triage."""
        if likelihood not in ("likely", "possible"):
            raise ValueError(f"likelihood must be likely or possible, not {likelihood!r}")
        status = TO_READ if likelihood == "likely" else TO_TRIAGE
        return self.add(doi, status, likelihood, found_in, title)

    def get(self, doi):
        row = self.db.execute("SELECT * FROM sources WHERE doi = ?", (clean_doi(doi),)).fetchone()
        return dict(row) if row else None

    def set_status(self, doi, status, reason=None, title=None):
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        with self.db:
            cur = self.db.execute(
                "UPDATE sources SET status = ?, reason = COALESCE(?, reason),"
                " title = COALESCE(?, title), updated_at = ? WHERE doi = ?",
                (status, reason, title, time.time(), clean_doi(doi)))
        if cur.rowcount == 0:
            raise KeyError(doi)

    def queue(self, status, limit=None):
        """Sources with this status, highest priority first, oldest first."""
        sql = f"SELECT * FROM sources WHERE status = ? ORDER BY {_PRIORITY}, added_at, doi"
        args = [status]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def counts(self):
        rows = self.db.execute("SELECT status, COUNT(*) FROM sources GROUP BY status")
        return {s: n for s, n in rows}

    # --- extracted records ----------------------------------------------------

    def save_compounds(self, doi, compounds, uncertain=None):
        """Store a read paper's results and set its status (W3). Returns the status.

        compounds: list of (rows, issues) from normalize_values.normalize_record, one per
        compound. A compound with issues goes to review; uncertain (P7c) sends all to review.
        """
        doi = clean_doi(doi)
        targets = []
        with self.db:
            self.db.execute("DELETE FROM records WHERE source_doi = ?", (doi,))
            for rows, issues in compounds:
                reasons = ([f"uncertain paper: {uncertain}"] if uncertain else []) + list(issues)
                target = "review" if reasons else "data"
                targets.append(target)
                for row in rows:
                    self.db.execute(
                        f"INSERT INTO records (source_doi, target, review_reason, {', '.join(_RECORD_COLS)})"
                        f" VALUES (?, ?, ?, {', '.join('?' * len(_RECORD_COLS))})",
                        [doi, target, "; ".join(reasons) or None] + [row[c] for c in _RECORD_COLS])
        if not targets:
            status, reason = IRRELEVANT, "no compound with structure and energy data (P7a)"
        elif all(t == "data" for t in targets):
            status, reason = DONE, None
        elif all(t == "review" for t in targets):
            status, reason = REVIEW, uncertain
        else:
            status, reason = PARTIAL, None
        self.set_status(doi, status, reason)
        return status

    def records(self, target):
        rows = self.db.execute("SELECT * FROM records WHERE target = ? ORDER BY id", (target,))
        return [dict(r) for r in rows]

    # --- exports (W4) -----------------------------------------------------------

    def export(self, out_dir=DATA_DIR / "output"):
        """Write data.csv, review.csv, irrelevant.csv, reading_list.csv and wishlist.csv."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        _write(out / "data.csv", COLUMNS, self.records("data"))
        _write(out / "review.csv", COLUMNS + ["review_reason"], self.records("review"))
        _write(out / "irrelevant.csv", ["doi", "title", "reason", "found_in"], self.queue(IRRELEVANT))
        everything = [dict(r) for r in self.db.execute(f"SELECT * FROM sources ORDER BY added_at, doi")]
        _write(out / "reading_list.csv", ["doi", "title", "status", "likelihood", "found_in", "reason"], everything)
        wishes = [dict(s, link=f"https://doi.org/{s['doi']}") for s in self.queue(AWAITING_PDF)]
        _write(out / "wishlist.csv", ["doi", "title", "link", "found_in", "reason"], wishes)
        return out


def _write(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r[c]) for c in columns})
