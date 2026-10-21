"""Human review of flagged records (RULES.md W2a, W3): decisions typed into review.csv.

Open data/output/review.csv (Excel is fine), fill in the decision column for any rows,
save, and run `python -m energy_extractor review`. `export`, `run` and `reprocess` apply
filled-in decisions too, before they rewrite the file.

    accept   the record is right, or right after your edits to its cells -> data table
    reject   the record is wrong -> rejected.csv (kept for the record)
    reread   read the paper again; write in note what to look out for (given to the extractor)

Decisions on accept/reject are final: reading or reprocessing the paper again keeps them.
"""
import csv

from .checks import check_rows
from .ledger import DATA_DIR, TO_READ
from .normalize_values import COLUMNS, NA
from .structures import canonicalize

REVIEW_FILE = DATA_DIR / "output" / "review.csv"
DECISIONS = {"accept": "accept", "a": "accept", "reject": "reject", "r": "reject", "reread": "reread"}
NUMBERS = ("homo_dft", "lumo_dft", "ip", "ea")
EDITABLE = [c for c in COLUMNS if c != "source_doi"]


def read_decisions(path=REVIEW_FILE):
    """Rows of review.csv with a decision filled in."""
    if not path.exists():
        return []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # saved by Excel as plain "CSV" in the Windows code page
        text = raw.decode("cp1252")
    return [r for r in csv.DictReader(text.splitlines()) if (r.get("decision") or "").strip()]


def _clean(column, value, old):
    """A cell as the person left it -> (value to store, error)."""
    value = (value or "").strip() or NA
    if column in NUMBERS:
        if value.lower() == NA:
            return NA, None
        try:
            new = float(value.replace("−", "-"))
        except ValueError:
            return None, f"{column}: {value!r} is not a number"
        try:  # Excel turns "5.20" into "5.2": only a different number is an edit
            if float(old) == new:
                return old, None
        except (TypeError, ValueError):
            pass
        return f"{new:.2f}", None
    if column == "smiles" and value != old and value != NA:
        canon, err = canonicalize(value)
        return (canon, None) if canon else (None, f"smiles: {err}")
    return value, None


def apply(ledger, path=REVIEW_FILE):
    """Apply the decisions in review.csv. Returns one message per decision."""
    return apply_rows(ledger, read_decisions(path))


def apply_rows(ledger, rows):
    """Apply decisions given as review.csv rows ({"id", "decision", "note", edited columns})."""
    messages, touched, reread = [], set(), set()
    for row in rows:
        decision = DECISIONS.get(row["decision"].strip().lower())
        note = (row.get("note") or "").strip() or None
        try:
            rec = ledger.record(int(row["id"]))
        except (KeyError, TypeError, ValueError):
            rec = None
        if rec is None or rec["target"] != "review" or rec["reviewed"] == "reread":
            continue  # already applied, or a row that isn't ours
        label = f"#{rec['id']} {rec['name']}"
        if decision is None:
            messages.append(f"{label}: unknown decision {row['decision']!r} (use accept, reject or reread)")
            continue

        if decision == "reject":
            ledger.update_record(rec["id"], target="rejected", reviewed="rejected", review_note=note)
            messages.append(f"{label}: rejected")
        elif decision == "reread":
            ledger.add_hint(rec["source_doi"], note or "a reviewer asked for this paper to be read again")
            ledger.update_record(rec["id"], reviewed="reread", review_note=note)
            ledger.set_status(rec["source_doi"], TO_READ, "reread requested in review")
            reread.add(rec["source_doi"])
            messages.append(f"{label}: paper {rec['source_doi']} back on the reading list")
            continue
        else:
            edits, errors = {}, []
            for column in EDITABLE:
                if column not in row:
                    continue
                value, err = _clean(column, row[column], rec[column])
                if err:
                    errors.append(err)
                elif value != rec[column]:
                    edits[column] = value
            if errors:
                why = "could not apply your accept: " + "; ".join(errors)
                ledger.update_record(rec["id"], review_note=why + (f" (your note: {note})" if note else ""))
                messages.append(f"{label}: {why}")
                continue
            reviewed = "corrected" if edits else "accepted"
            ledger.update_record(rec["id"], **edits, target="data", reviewed=reviewed, review_note=note)
            messages.append(f"{label}: {reviewed}" + (f" ({', '.join(edits)})" if edits else ""))
            for warning in check_rows([rec | edits]):
                messages.append(f"{label}: note, {warning}")
        touched.add(rec["source_doi"])

    for doi in touched - reread:  # a paper sent back for reading stays on the reading list
        ledger.refresh_status(doi)
    if touched and (n := ledger.sync_names()):
        messages.append(f"W2a: names synced on {n} record(s) that share a SMILES")
    return messages
