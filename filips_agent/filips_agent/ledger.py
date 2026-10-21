"""SQLite ledger of every source the agent has seen (RULES.md W1, W3, W6, W7).

A DOI is added once and never evaluated again. Statuses:

    to_triage     "possible" reference; its abstract still has to be judged (W1)
    to_read       on the reading list
    awaiting_pdf  relevant, but no full text yet; listed in the wish list (W6)
    done          every compound record went to the data table (W3)
    partial       some records in data, some in review
    review        every record in review, incl. papers marked uncertain (P7c)
    irrelevant    rejected at the abstract or paper stage

Records are in the data, review or rejected table. A record a person has decided on in
review (accepted, corrected or rejected) is never replaced when the paper is read again.
"""
import csv
import sqlite3
import time
from pathlib import Path

from .checks import name_key
from .dois import clean_doi
from .normalize_values import COLUMNS, NA

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
    target TEXT NOT NULL CHECK (target IN ('data', 'review', 'rejected')),
    review_reason TEXT,
    reviewed TEXT,               -- a person's decision: accepted, corrected, rejected or reread
    review_note TEXT,            -- what the person wrote, or why their decision couldn't be applied
    {", ".join(f"{c} TEXT" for c in _RECORD_COLS)}
);
CREATE TABLE IF NOT EXISTS hints (
    doi TEXT NOT NULL REFERENCES sources(doi),
    note TEXT NOT NULL,          -- for the extractor when the paper is read again
    added_at REAL NOT NULL
);
"""
FINAL = ("accepted", "corrected", "rejected")  # decisions that re-reading must not undo

# seeds first, then likely, then possible references promoted by triage
_PRIORITY = "CASE likelihood WHEN 'likely' THEN 1 WHEN 'possible' THEN 2 ELSE 0 END"


class Ledger:
    def __init__(self, path=DATA_DIR / "ledger.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._upgrade()
        self.db.executescript(_SCHEMA)

    def _upgrade(self):
        """Ledgers made before review existed: rebuild records with the new columns."""
        row = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'records'").fetchone()
        if row is None or "rejected" in row[0]:
            return
        cols = ", ".join(["id", "source_doi", "target", "review_reason"] + _RECORD_COLS)
        with self.db:
            self.db.execute("ALTER TABLE records RENAME TO records_old")
            self.db.executescript(_SCHEMA)
            self.db.execute(f"INSERT INTO records ({cols}) SELECT {cols} FROM records_old")
            self.db.execute("DROP TABLE records_old")

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
        decided = [r for r in self._records_of(doi) if r["reviewed"] in FINAL]
        with self.db:
            self.db.execute("DELETE FROM records WHERE source_doi = ? AND (reviewed IS NULL OR reviewed"
                            f" NOT IN ({', '.join('?' * len(FINAL))}))", (doi, *FINAL))
            for rows, issues in compounds:
                if rows and any(_same_compound(rows[0], r) for r in decided):
                    continue  # a person already decided on this compound
                reasons = ([f"uncertain paper: {uncertain}"] if uncertain else []) + list(issues)
                target = "review" if reasons else "data"
                for row in rows:
                    self.db.execute(
                        f"INSERT INTO records (source_doi, target, review_reason, {', '.join(_RECORD_COLS)})"
                        f" VALUES (?, ?, ?, {', '.join('?' * len(_RECORD_COLS))})",
                        [doi, target, "; ".join(reasons) or None] + [row[c] for c in _RECORD_COLS])
        return self.refresh_status(doi, uncertain)

    def refresh_status(self, doi, uncertain=None):
        """W3: a paper's status follows from its records (rejected ones don't count)."""
        targets = [r["target"] for r in self._records_of(doi)]
        kept = [t for t in targets if t != "rejected"]
        if not kept:
            status = IRRELEVANT
            reason = ("all records rejected in review" if targets
                      else "no compound with structure and energy data (P7a)")
        elif all(t == "data" for t in kept):
            status, reason = DONE, None
        elif all(t == "review" for t in kept):
            status, reason = REVIEW, uncertain
        else:
            status, reason = PARTIAL, None
        self.set_status(doi, status, reason)
        return status

    def _records_of(self, doi):
        rows = self.db.execute("SELECT * FROM records WHERE source_doi = ? ORDER BY id", (clean_doi(doi),))
        return [dict(r) for r in rows]

    def record(self, record_id):
        row = self.db.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return dict(row) if row else None

    def update_record(self, record_id, **fields):
        cols = ", ".join(f"{c} = ?" for c in fields)
        with self.db:
            self.db.execute(f"UPDATE records SET {cols} WHERE id = ?", (*fields.values(), record_id))

    def sync_names(self):
        """W2a: records in the data table with the same SMILES list all of each other's names."""
        groups = {}
        for r in self.records("data"):
            if r["smiles"] not in (None, NA):
                groups.setdefault(r["smiles"], []).append(r)
        changed = 0
        for records in groups.values():
            names = [n for r in records if r["name"] not in (None, NA) for n in r["name"].split(", ")]
            merged = ", ".join(dict.fromkeys(names))
            for r in records:
                if merged and r["name"] != merged:
                    self.update_record(r["id"], name=merged)
                    changed += 1
        return changed

    # --- notes for the extractor ------------------------------------------------------

    def add_hint(self, doi, note):
        with self.db:
            self.db.execute("INSERT INTO hints VALUES (?, ?, ?)", (clean_doi(doi), note, time.time()))

    def hints(self, doi):
        rows = self.db.execute("SELECT note FROM hints WHERE doi = ? ORDER BY added_at", (clean_doi(doi),))
        return list(dict.fromkeys(r[0] for r in rows))

    def records(self, target):
        rows = self.db.execute("SELECT * FROM records WHERE target = ? ORDER BY id", (target,))
        return [dict(r) for r in rows]

    # --- exports (W4) -----------------------------------------------------------

    def export(self, out_dir=DATA_DIR / "output"):
        """Write data.csv, review.csv, irrelevant.csv, reading_list.csv and wishlist.csv."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        _write(out / "data.csv", COLUMNS, self.records("data"))
        review = [dict(r, note=r["review_note"]) for r in self.records("review")]
        _write(out / "review.csv", REVIEW_COLUMNS, review, excel=True)
        _write(out / "rejected.csv", COLUMNS + ["review_reason", "review_note"], self.records("rejected"))
        _write(out / "irrelevant.csv", ["doi", "title", "reason", "found_in"], self.queue(IRRELEVANT))
        everything = [dict(r) for r in self.db.execute(f"SELECT * FROM sources ORDER BY added_at, doi")]
        _write(out / "reading_list.csv", ["doi", "title", "status", "likelihood", "found_in", "reason"], everything)
        wishes = [dict(s, link=f"https://doi.org/{s['doi']}") for s in self.queue(AWAITING_PDF)]
        _write(out / "wishlist.csv", ["doi", "title", "link", "found_in", "reason"], wishes)
        return out


# review.csv is meant to be edited: fill in decision (and note), then run the review command
REVIEW_COLUMNS = ["id", "decision", "note"] + COLUMNS + ["review_reason"]


def _same_compound(row, record):
    """Same SMILES, or a name in common (ignoring case, spaces and hyphens)."""
    if row["smiles"] not in (None, NA) and row["smiles"] == record["smiles"]:
        return True

    def names(r):
        return {name_key(n) for n in (r["name"] or "").split(", ") if n and n != NA}
    return bool(names(row) & names(record))


def _write(path, columns, rows, excel=False):
    # utf-8-sig: Excel only recognises UTF-8 (Greek letters, primes) with the byte-order mark
    with open(path, "w", encoding="utf-8-sig" if excel else "utf-8", newline="") as f:
        w = csv.DictWriter(f, columns, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r[c]) for c in columns})
