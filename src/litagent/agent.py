"""The extraction pipeline (first pass). The review agent (review_agent.py) works on what it flags.

For each paper:
  1. search   - resolve queries to open-access papers (tools/search.py, tools/europepmc.py)
  2. fetch    - download PDFs, or Europe PMC full text (JATS XML) and SI files (tools/fetch.py)
  3. store    - parse every file into the paper store, once (store.py, tools/parse_pdf.py, tools/jats.py)
  4. filter   - keep only chunks likely to contain the target data (tools/prefilter.py)
  5. extract  - ask the model for the fields in schema.json (tools/extract.py)
  6. validate - verify the evidence quote, check the schema, run domain post-processing, flag for review
  7. repair   - one targeted retry for flagged records with a repair rule (tools/repair.py)
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import domains
from .models import BudgetExceeded
from .store import PaperStore
from .tools.extract import build_system_prompt, extract
from .tools.fetch import fetch
from .tools.parse_pdf import SUPPORTED, parse_document
from .tools.prefilter import select
from .tools.repair import GENERIC, repair
from .tools.search import search

try:
    import jsonschema
except ImportError:              # optional
    jsonschema = None


def gather_sources(cfg: dict, root: Path) -> list:
    """Return a list of sources: {"path", "meta", "paper_key", "role"}.

    Local files are grouped into papers by paper_groups. Europe PMC results bring their own
    grouping: the full text and its SI files share the PMCID as paper_key.
    """
    groups = cfg.get("paper_groups") or {}
    download = str(root / cfg.get("download_dir", ".cache/papers"))
    sources = []
    for folder in cfg.get("local_folders", []) or []:
        base = (root / folder) if not Path(folder).is_absolute() else Path(folder)
        for p in sorted(base.rglob("*")):
            if p.suffix.lower() in SUPPORTED and not p.name.startswith(("~$", ".")):
                sources.append({"path": p, "meta": {"source": "local file"},
                                "paper_key": group_of(p.name, groups), "role": None})
    for q in cfg.get("queries", []) or []:
        source = q.get("source", "chemrxiv") if isinstance(q, dict) else "chemrxiv"
        term = q["query"] if isinstance(q, dict) else q
        limit = q.get("limit", 10) if isinstance(q, dict) else 10
        print(f"Searching {source} for '{term}' (limit {limit}) ...")
        try:
            hits = search(term, source=source, limit=limit)
        except Exception as exc:
            print(f"  [warn] search failed: {exc}")
            continue
        for item in hits:
            meta = {k: item.get(k) for k in ("source", "doi", "title", "license", "authors")}
            meta["year"] = item.get("published")
            if item.get("pmcid"):
                sources += europepmc_sources(item, download, q.get("include_si", True))
                continue
            path = fetch(item, download)
            if path:
                sources.append({"path": path, "meta": meta, "paper_key": item.get("doi") or path.name, "role": "main"})
    return sources


def _schema_problems(rec: dict, schema: dict) -> list:
    if jsonschema is None:
        return []
    model_fields = {k: v for k, v in schema.get("properties", {}).items() if not v.get("x-provenance")}
    sub = {"type": "object", "properties": model_fields}
    validator = getattr(jsonschema, "Draft202012Validator", None) or jsonschema.Draft7Validator  # older jsonschema
    v = validator(sub)
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(rec)]


def europepmc_sources(item: dict, download_dir: str, include_si: bool = True) -> list:
    """Download one Europe PMC result (full text + SI) and return it as sources for run()."""
    from .tools.europepmc import fetch_fulltext, fetch_supplementary
    meta = {k: item.get(k) for k in ("source", "doi", "title", "license", "authors")}
    meta["year"] = item.get("published")
    try:
        xml = fetch_fulltext(item["pmcid"], download_dir)
        si = fetch_supplementary(item["pmcid"], download_dir) if xml and include_si else []
    except Exception as exc:
        print(f"  [warn] Europe PMC download failed for {item['pmcid']}: {exc}")
        return []
    if not xml:
        return []
    return ([{"path": xml, "meta": meta, "paper_key": item["pmcid"], "role": "main"}]
            + [{"path": p, "meta": meta, "paper_key": item["pmcid"], "role": "si"} for p in si])


def store_path(cfg: dict, root: Path) -> str:
    """Paper store location: store.path in the profile, else ~/.cache/litagent/papers.sqlite.

    The default is outside the repository on purpose: a database inside a cloud-synced folder
    (OneDrive, Dropbox, iCloud) can be corrupted by the sync client.
    """
    p = (cfg.get("store") or {}).get("path")
    if not p:
        return str(Path.home() / ".cache" / "litagent" / "papers.sqlite")
    p = Path(p).expanduser()
    return str(p if p.is_absolute() else root / p)


def group_of(filename: str, groups: dict) -> str:
    """Files whose names contain any listed fragment belong to the same paper (main text + SI)."""
    for name, fragments in (groups or {}).items():
        if any(str(f).lower() in filename.lower() for f in fragments):
            return name
    return filename


class _NullLog:
    context: dict = {}

    @contextmanager
    def stage(self, name, **fields):
        yield

    def event(self, *a, **k):
        pass


def run(papers_config: dict, schema: dict, model, root: Path | None = None, dry_run: bool = False,
        log=None, store: PaperStore | None = None, sources: list | None = None, progress=None) -> tuple:
    """Process every source. Returns (records, files_summary).

    `log` is an optional RunLogger. `store` is the paper store to use; by default the one named in
    the profile (store.path, default .cache/papers.sqlite). The review agent reuses the same store.
    """
    log = log or _NullLog()
    progress = progress or (lambda event, **info: None)
    root = root or Path.cwd()
    cfg = papers_config
    store = store or PaperStore(store_path(cfg, root))
    evidence_key = cfg.get("evidence_field", "evidence")
    system_prompt = build_system_prompt(schema, cfg["task"], cfg.get("instructions", ""), evidence_key)
    enrich = domains.load(cfg.get("domain"))
    paper_flags = domains.load_paper_flags(cfg.get("domain"))
    passage_flags = domains.load_hook(cfg.get("domain"), "passage_flags", lambda text: {})
    rep_cfg = cfg.get("repair") or {}
    rep_rules = {**GENERIC, **(domains.load_hook(cfg.get("domain"), "REPAIRS", {}) or {})}
    context_fn = domains.load_hook(cfg.get("domain"), "paper_context", lambda text: "")
    papers = {}                     # source_file -> (full text, context for repairs)
    pf = cfg["prefilter"]
    groups = cfg.get("paper_groups") or {}

    records, files, lookups = [], [], {}
    if sources is None:
        with log.stage("gather"):
            sources = gather_sources(cfg, root)
    print(f"{len(sources)} file(s) to process")
    progress("sources", n=len(sources))

    # Store: parse every file once (unchanged files reuse their stored chunks)
    ingested = []
    for src in sources:
        path, meta = Path(src["path"]), src["meta"]
        progress("parse", file=path.name)
        try:
            with log.stage("parse", file=path.name):
                pid, fid = store.ingest_file(path, src["paper_key"], meta, src["role"],
                                             pf.get("chunk_chars", 2500), pf.get("table_rows_per_chunk"))
        except Exception as exc:
            print(f"- {path.name}: [skip] could not read ({exc})")
            files.append({"file": path.name, "status": f"unreadable: {exc}"})
            continue
        ingested.append((path, meta, pid, fid))
    # Facts about whole papers (all files): label -> sequence tables, paper-level flags, repair context
    paper_info = {}
    for pid in dict.fromkeys(p for _, _, p, _ in ingested):
        full_text = store.paper_text(pid)
        found = {}
        for f in store.files(pid):
            for k, v in label_lookup(store.file_chunks(f["file_id"]), f["name"]).items():
                found.setdefault(k, v)
        lookups[pid] = found
        paper_info[pid] = (full_text, paper_flags(full_text),
                           context_fn(full_text) if rep_cfg.get("enabled") else "")

    for n_file, (path, meta, pid, fid) in enumerate(ingested, start=1):
        frow, prow = store.file(fid), store.paper(pid)
        chunks = store.file_chunks(fid)
        doc_title = meta.get("title") or prow["title"] or frow["title"] or path.stem
        doc_doi = meta.get("doi") or prow["doi"] or frow["doi"] or ""
        group = pid
        full_text, flags, context = paper_info[pid]
        papers[path.name] = (full_text, context)
        found_labels = label_lookup(chunks, path.name)
        chosen = select(chunks, pf)
        summary = {"file": path.name, "paper_group": prow["paper_key"], "paper_id": pid, "title": doc_title,
                   "doi": doc_doi, "source": meta.get("source"), "chunks_total": len(chunks), "chunks_sent": len(chosen),
                   "sequence_labels_found": len(found_labels), "records": 0, "status": "ok"}
        print(f"- {path.name}: {len(chunks)} chunks, {len(chosen)} selected"
              + (f", {len(found_labels)} label->sequence entries" if found_labels else ""))

        if dry_run:
            for c in chosen:
                print(f"    [{c.score:>2}] {c.label}: {c.text[:100].replace(chr(10), ' ')}")
            files.append(summary)
            continue

        stop = False
        for n_chunk, c in enumerate(chosen, start=1):
            progress("extract", file=path.name, file_no=n_file, files=len(ingested), chunk_no=n_chunk,
                     chunks=len(chosen), location=c.label)
            log.context = {"phase": "extract", "file": path.name, "location": c.label}
            try:
                with log.stage("extract", file=path.name, location=c.label):
                    found = extract(c.text, schema, model, system_prompt, doc_title, c.label, c.kind,
                                    evidence_key, cfg.get("llm", {}).get("max_output_tokens", 4000))
            except BudgetExceeded as exc:
                print(f"  [stop] {exc}; saving partial results")
                summary["status"] = "stopped: call budget reached"
                stop = True
                break
            except Exception as exc:
                print(f"  [warn] {c.label}: {exc}")
                continue
            cov_cfg = cfg.get("coverage")
            if cov_cfg and c.kind == "table":
                cov = table_coverage(c.text, found, cov_cfg)
                if cov is not None:
                    log.event("table_coverage", file=path.name, location=c.label, **cov)
                    if cov["coverage"] < cov_cfg.get("min_coverage", 0.8):
                        summary.setdefault("incomplete_tables", [])
                        summary["incomplete_tables"].append(f"{c.label} ({cov['coverage']:.0%})")
                        for rec in found:
                            rec["_problems"] = [f"table only {cov['coverage']:.0%} extracted "
                                                f"({cov['matched']}/{cov['cells']} values)"]
            pflags = passage_flags(c.text)
            for rec in found:
                rec.update(pflags)
                rec["_problems"] = rec.get("_problems", []) + _schema_problems(rec, schema)
                rec.update(doi=doc_doi, paper_title=doc_title, source=meta.get("source"), paper_id=pid,
                           source_file=path.name, page=c.page, location=c.label, chunk_id=c.chunk_id,
                           _group=group, _passage=c.text,
                           model=getattr(model, "model", ""), **flags,
                           extracted_at=datetime.now().isoformat(timespec="seconds"))
                records.append(rec)
            summary["records"] += len(found)
        if isinstance(summary.get("incomplete_tables"), list):
            summary["incomplete_tables"] = "; ".join(summary["incomplete_tables"])
        files.append(summary)
        if stop:
            break

    # Post-processing: link label-only records to sequences, then domain enrichment
    log.context = {}
    out = []
    for rec in records:
        link_sequence(rec, lookups.get(rec.pop("_group"), {}))
        problems = rec.pop("_problems")
        rec = enrich(rec)
        if problems:
            rec["needs_review"] = True
            rec["review_reason"] = "; ".join(filter(None, [rec.get("review_reason"), *problems]))
        out.append(rec)

    # Repair: one targeted retry per flagged record whose problem has a repair rule
    if rep_cfg.get("enabled") and not dry_run:
        fixed = rejected = 0
        for i, rec in enumerate(out):
            progress("repair", record_no=i + 1, records=len(out))
            log.context = {"phase": "repair", "file": rec.get("source_file"), "location": rec.get("location")}
            full_text, context = papers.get(rec.get("source_file"), ("", ""))
            try:
                with log.stage("repair", file=rec.get("source_file"), location=rec.get("location")):
                    new = repair(rec, rec.get("_passage", ""), full_text, context, schema, model, enrich,
                                 rep_rules, evidence_key, rep_cfg.get("max_attempts", 1))
            except BudgetExceeded as exc:
                print(f"  [stop] repair: {exc}")
                break
            except Exception as exc:
                print(f"  [warn] repair {rec.get('peptide_name')}: {exc}")
                continue
            out[i] = new
            if new.get("repair_attempted"):
                fixed += new.get("repair_outcome") == "accepted"
                rejected += new.get("repair_outcome") != "accepted"
        if fixed or rejected:
            print(f"Repair: {fixed} record(s) corrected, {rejected} left for review")
            log.event("repair_summary", accepted=fixed, not_accepted=rejected)
    for rec in out:
        rec.pop("_passage", None)
    return dedupe(out, cfg.get("dedupe_keys")), files


# --- table coverage --------------------------------------------------------------

_NUM = re.compile(r"[-+]?\d*\.\d+|[-+]?\d+")


def table_coverage(text: str, records: list, cov_cfg: dict) -> dict | None:
    """Fraction of numeric table cells that appear as a value in the extracted records.

    The first column (row labels) and columns whose header matches exclude_columns
    (e.g. calculated values) are not counted. Returns None for tables with no numbers.
    """
    rows = [r.split(" | ") for r in text.split("\n") if " | " in r]
    if len(rows) < 2:
        return None
    header = rows[0]
    skip = re.compile(cov_cfg.get("exclude_columns", "$^"), re.I)
    keep = [i for i, h in enumerate(header) if i > 0 and not skip.search(h)]
    cells = []
    for r in rows[1:]:
        for i in keep:
            if i < len(r):
                m = _NUM.search(r[i].replace("−", "-"))
                if m:
                    cells.append(float(m.group(0)))
    if not cells:
        return None
    key = cov_cfg.get("value_field", "value")
    got = []
    for rec in records:
        try:
            got.append(float(rec.get(key)))
        except (TypeError, ValueError):
            pass
    matched = sum(1 for v in cells if any(abs(v - g) <= 1e-9 + 1e-6 * abs(v) for g in got))
    return {"cells": len(cells), "matched": matched, "coverage": round(matched / len(cells), 3)}


def missing_cells(text: str, records: list, cov_cfg: dict) -> list:
    """Table cells (row label, column, value) whose value is not in any record. Used by the review agent."""
    rows = [r.split(" | ") for r in text.split("\n") if " | " in r]
    if len(rows) < 2:
        return []
    header = rows[0]
    skip = re.compile(cov_cfg.get("exclude_columns", "$^"), re.I)
    keep = [i for i, h in enumerate(header) if i > 0 and not skip.search(h)]
    key = cov_cfg.get("value_field", "value")
    got = []
    for rec in records:
        try:
            got.append(float(rec.get(key)))
        except (TypeError, ValueError):
            pass
    out = []
    for r in rows[1:]:
        for i in keep:
            if i < len(r):
                m = _NUM.search(r[i].replace("−", "-"))
                if m and not any(abs(float(m.group(0)) - g) <= 1e-9 + 1e-6 * abs(float(m.group(0))) for g in got):
                    out.append((r[0].strip(), header[i].strip(), r[i].strip()))
    return out


# --- label -> sequence linking -----------------------------------------------

_LABEL_PREFIX = re.compile(r"^(variant|compound|cpd\.?|peptide|pep\.?|no\.?|entry|#)\s*", re.I)


def label_key(name) -> str | None:
    """'Variant 7', 'GLP-1 variant 7 (Run1)', 'compound 7', '7' -> '7'; other names lower-cased."""
    if name is None:
        return None
    s = re.sub(r"\([^)]*\)", "", str(name)).strip()
    m = re.search(r"\b(?:variant|compound|cpd\.?|peptide|pep\.?|entry|no\.?)\s*#?\s*(\d+[a-zA-Z]?)\b", s, re.I)
    if m:
        return m.group(1).lower()
    s = _LABEL_PREFIX.sub("", s)
    m = re.search(r"(\d+[a-zA-Z]?)\s*$", s)
    return (m.group(1) if m else s).lower() or None


def label_lookup(chunks: list, filename: str) -> dict:
    """Find tables with a 'sequence' column and map the first column (label) to the sequence."""
    found = {}
    for c in chunks:
        if c.kind != "table":
            continue
        rows = [r.split(" | ") for r in c.text.split("\n") if " | " in r]
        if not rows:
            continue
        header = [h.strip().lower() for h in rows[0]]
        seq_col = next((i for i, h in enumerate(header) if "sequence" in h), None)
        if seq_col is None or seq_col == 0:
            continue
        for r in rows[1:]:
            if len(r) > seq_col and r[0].strip() and r[seq_col].strip():
                key = label_key(r[0])
                if key and key not in found:
                    found[key] = (r[seq_col].strip(), f"{filename} {c.label}")
    return found


def link_sequence(rec: dict, lookup: dict) -> None:
    rec.setdefault("sequence_source", "this passage" if rec.get("sequence") else None)
    if rec.get("sequence") or not lookup:
        return
    key = label_key(rec.get("peptide_name"))
    if key in lookup:
        seq, where = lookup[key]
        rec["sequence"] = seq
        rec["sequence_source"] = f"linked by label from {where}"


def dedupe(records: list, keys: list | None) -> list:
    if not keys:
        return records
    seen, out = set(), []
    for r in records:
        k = tuple(str(r.get(f)) for f in keys)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out
