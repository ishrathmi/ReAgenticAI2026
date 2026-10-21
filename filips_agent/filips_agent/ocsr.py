"""Check the extractor's structures against the paper's own drawings (PLAN.md structures route 3).

For every compound of a read paper (from its saved extraction):
1. figures.find_figures: the figures whose captions name it or show structures/syntheses;
2. DECIMER Segmentation cuts out each chemical drawing on those pages; the label next to each
   drawing is read from the text layer or by OCR, and figures.pick chooses the compound's one;
3. DECIMER and MolScribe each read the drawing as SMILES;
4. the reading is confirmed against the reported formula (P3e) and OPSIN/PubChem, and compared
   with the extractor's SMILES: exact, core (same ring system, side chains differ) or different.

The image models live in their own environments (.venv-decimer, .venv-molscribe; see
ocsr/README.md) and run one after the other as subprocesses, so their memory is freed in between.
Results are cached per paper in data/cache/ocsr/. Nothing is moved between tables: the outcome
goes to structure_checks.csv and, for records in review, into their review reason.
"""
import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

from . import fetch, figures, resolve
from .dois import clean_doi, doi_slug
from .ledger import DATA_DIR
from .structures import canonicalize, formula_matches

ROOT = Path(__file__).resolve().parents[1]
WORKERS = ROOT / "ocsr"
CACHE_DIR = DATA_DIR / "cache" / "ocsr"
REPORT = DATA_DIR / "output" / "structure_checks.csv"
DPI = 300
MAX_FIGURES = 3  # per compound, best-scoring first
TOOLS = ("decimer", "molscribe")


def _python(venv):
    scripts = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return ROOT / venv / scripts


def available():
    """Which of the two image tools are installed."""
    return {"decimer": _python(".venv-decimer").exists(), "molscribe": _python(".venv-molscribe").exists()}


def _run_worker(tool, job):
    """Run one worker in its own environment; returns its JSON result."""
    venv = ".venv-decimer" if tool == "decimer" else ".venv-molscribe"
    with tempfile.TemporaryDirectory() as tmp:
        job_path, result_path = Path(tmp) / "job.json", Path(tmp) / "result.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        env = dict(os.environ, TF_CPP_MIN_LOG_LEVEL="2", PYTHONUTF8="1")
        done = subprocess.run([str(_python(venv)), str(WORKERS / f"{tool}_worker.py"), str(job_path),
                               str(result_path)], capture_output=True, text=True, env=env)
        if done.returncode != 0:
            raise RuntimeError(f"{tool} worker failed:\n{done.stderr[-2000:]}")
        return json.loads(result_path.read_text(encoding="utf-8"))


# --- comparing structures ----------------------------------------------------------------

def core(smiles):
    """Ring systems and the links between them (Murcko scaffold), no side chains or stereo."""
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    try:
        return Chem.MolToSmiles(MurckoScaffold.GetScaffoldForMol(mol), isomericSmiles=False)
    except Exception:
        return None


def compare(found, expected):
    """How a structure read from an image relates to another: exact, core, different or None."""
    if not found or not expected:
        return None
    if resolve.compare_key(found) == resolve.compare_key(expected):
        return "exact"
    a, b = core(found), core(expected)
    return "core" if a and a == b else "different"


_RANK = {"exact": 3, "core": 2, "different": 1, None: 0}


def judge(drawing, compound):
    """Confirm one drawing against the compound; returns a dict for the report."""
    readings = {}
    for tool in TOOLS:
        canon, _ = canonicalize(drawing["smiles"].get(tool)) if drawing["smiles"].get(tool) else (None, None)
        if canon:
            readings[tool] = canon
    agent = compound.get("smiles")
    vs_agent = {tool: compare(s, agent) for tool, s in readings.items()}
    formula = {tool: formula_matches(s, compound.get("reported_formula")) for tool, s in readings.items()}
    lookups = []
    for name in compound["names"][:resolve.MAX_NAMES]:
        for source in resolve.SOURCES:
            lookups += [h["smiles"] for h in resolve.lookup(source, name) or []]
    vs_lookup = {tool: max((compare(s, h) for h in lookups), key=_RANK.get, default=None)
                 for tool, s in readings.items()}

    best = max(vs_agent.values(), key=_RANK.get, default=None)
    if not readings:
        verdict = "unreadable"
    elif best == "exact":
        verdict = "matches"
    elif best == "core":
        verdict = "core matches (side chains differ)"
    else:
        # does the reading fit the paper's formula or the compound's name (OPSIN/PubChem)?
        confirmed = any(f for f in formula.values()) or any(v in ("exact", "core") for v in vs_lookup.values())
        if agent is None:
            verdict = "candidate structure" if confirmed else "unconfirmed reading"
        else:
            verdict = "agent's structure may be wrong" if confirmed else "different compound?"
    agree = len(readings) == 2 and resolve.compare_key(readings["decimer"]) == resolve.compare_key(readings["molscribe"])
    return {"verdict": verdict, "readings": readings, "vs_agent": vs_agent, "formula": formula,
            "vs_lookup": vs_lookup, "tools_agree": agree}


# --- finding the drawings ------------------------------------------------------------------

def _documents(doi, si):
    text = fetch.get_fulltext(doi)
    if not text:
        return []
    docs = [("main", text["main"])]
    if si:
        docs += [(f"SI{i}", p) for i, p in enumerate(text["si"], 1) if p.suffix.lower() == ".pdf"]
    return docs


def _read_drawings(doi, tag, pdf, pages, refresh=False):
    """Segment, OCR and read the drawings on these pages, with a cache per document page."""
    cache_dir = CACHE_DIR / doi_slug(doi)
    cache_file = cache_dir / f"{tag}.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() and not refresh else {}
    todo = [p for p in pages if str(p) not in cache]
    if todo:
        doc = pymupdf.open(pdf)
        out_dir = cache_dir / tag
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = []
        for p in todo:
            image = out_dir / f"page{p + 1}.png"
            doc[p].get_pixmap(dpi=DPI).save(image)
            jobs.append({"page": p, "image": str(image)})
        found = _run_worker("decimer", {"pages": jobs, "out_dir": str(out_dir),
                                        "label_gap": int(figures.LABEL_GAP * DPI / 72)})
        if found and available()["molscribe"]:
            read = {r["image"]: r for r in _run_worker("molscribe", {"images": [d["image"] for d in found]})}
            for d in found:
                d["molscribe"] = read.get(d["image"], {}).get("molscribe")
        for p in todo:
            cache[str(p)] = [d for d in found if d["page"] == p]
        cache_file.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return cache


def _drawings_on(doc, page, cached):
    scale = 72 / DPI
    out = []
    for d in cached.get(str(page), []):
        rect = tuple(v * scale for v in d["bbox"])
        labels = figures.text_labels(doc[page], rect) + d.get("ocr", [])
        out.append(figures.Drawing(page, rect, d["image"], labels, {t: d.get(t) for t in TOOLS}))
    return out


def check_paper(doi, si=False, refresh=False):
    """Image check of every compound in a read paper. Returns report rows."""
    doi = clean_doi(doi)
    saved = DATA_DIR / "extractions" / f"{doi_slug(doi)}.json"
    if not saved.exists():
        return []
    compounds = []
    for c in json.loads(saved.read_text(encoding="utf-8"))["result"]["compounds"]:
        smiles, _ = canonicalize(c["smiles"]) if c["smiles"] else (None, None)
        names = c["names"] + ([c["label_in_paper"]] if c["label_in_paper"] else [])
        compounds.append({"names": names, "smiles": smiles, "reported_formula": c["reported_formula"],
                          "location": c.get("structure_location")})  # older extractions don't have it

    rows = []
    for tag, pdf in _documents(doi, si):
        doc = pymupdf.open(pdf)
        plan = {}  # compound index -> its figures
        for i, c in enumerate(compounds):
            plan[i] = figures.find_figures(doc, c["names"], c["location"] if tag == "main" else None)[:MAX_FIGURES]
        pages = sorted({f.page for figs in plan.values() for f in figs})
        if not pages:
            continue
        cached = _read_drawings(doi, tag, pdf, pages, refresh)
        for i, c in enumerate(compounds):
            choice = None
            for fig in plan[i]:
                in_fig = [d for d in _drawings_on(doc, fig.page, cached) if figures.in_region(d.rect, fig.region)]
                chosen, how = figures.pick(in_fig, c["names"], fig.kind)
                if chosen and (choice is None or (how == "label" and choice[2] != "label")):
                    choice = (fig, chosen, how)
                if choice and choice[2] == "label":
                    break
            if choice is None:
                continue
            fig, chosen, how = choice
            for d in chosen:
                j = judge({"smiles": d.smiles}, c)
                rows.append({"doi": doi, "compound": c["names"][0] if c["names"] else "?", "document": tag,
                             "figure": fig.name, "picked_by": how, "label_found": "; ".join(d.labels)[:120],
                             "image": d.image, "verdict": j["verdict"],
                             "tools_agree": "yes" if j["tools_agree"] else "no",
                             "decimer": d.smiles.get("decimer") or "", "molscribe": d.smiles.get("molscribe") or "",
                             "vs_agent": ", ".join(f"{t}: {v}" for t, v in j["vs_agent"].items() if v),
                             "formula": ", ".join(f"{t}: {'ok' if v else 'mismatch'}"
                                                  for t, v in j["formula"].items() if v is not None)})
    return rows


REPORT_COLUMNS = ["doi", "compound", "verdict", "tools_agree", "figure", "document", "picked_by", "label_found",
                  "vs_agent", "formula", "decimer", "molscribe", "image"]


def write_report(rows, path=REPORT):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, REPORT_COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return path


def note_in_review(ledger, rows):
    """Add each compound's image result to the review reason of its records in review."""
    best = {}
    for r in rows:
        key = (r["doi"], r["compound"])
        if key not in best or _VERDICT_RANK.get(r["verdict"], 0) > _VERDICT_RANK.get(best[key]["verdict"], 0):
            best[key] = r
    for (doi, compound), r in best.items():
        for rec in ledger.records("review"):
            if rec["source_doi"] != doi or not figures.mentions(rec["name"] or "", compound):
                continue
            reason = "; ".join(p for p in (rec["review_reason"] or "").split("; ") if not p.startswith("image check:"))
            note = f"image check: {r['verdict']} ({r['figure']}, {r['picked_by']})"
            ledger.update_record(rec["id"], review_reason="; ".join(p for p in (reason, note) if p))


_VERDICT_RANK = {"matches": 7, "core matches (side chains differ)": 6, "agent's structure may be wrong": 5,
                 "candidate structure": 4, "different compound?": 3, "unconfirmed reading": 2, "unreadable": 1}
