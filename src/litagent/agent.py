"""The agent loop.

For each paper:
  1. search   - resolve queries to open-access papers (tools/search.py)
  2. fetch    - download their PDFs (tools/fetch.py)
  3. parse    - convert PDF/XLSX/DOCX to text and table chunks (tools/parse_pdf.py)
  4. filter   - keep only chunks likely to contain the target data (tools/prefilter.py)
  5. extract  - ask the model for the fields in schema.json (tools/extract.py)
  6. validate - verify the evidence quote, check the schema, run domain post-processing, flag for review
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import domains
from .models import BudgetExceeded
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
    """Return a list of (path, metadata) for local files and downloaded search results."""
    sources = []
    for folder in cfg.get("local_folders", []) or []:
        base = (root / folder) if not Path(folder).is_absolute() else Path(folder)
        for p in sorted(base.rglob("*")):
            if p.suffix.lower() in SUPPORTED and not p.name.startswith("~$"):
                sources.append((p, {"source": "local file"}))
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
            path = fetch(item, cfg.get("download_dir", ".cache/papers"))
            if path:
                sources.append((path, {"source": item["source"], "doi": item["doi"], "title": item["title"]}))
    return sources


def _schema_problems(rec: dict, schema: dict) -> list:
    if jsonschema is None:
        return []
    model_fields = {k: v for k, v in schema.get("properties", {}).items() if not v.get("x-provenance")}
    sub = {"type": "object", "properties": model_fields}
    validator = getattr(jsonschema, "Draft202012Validator", None) or jsonschema.Draft7Validator  # older jsonschema
    v = validator(sub)
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(rec)]


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
        log=None) -> tuple:
    """Process every source. Returns (records, files_summary). `log` is an optional RunLogger."""
    log = log or _NullLog()
    root = root or Path.cwd()
    cfg = papers_config
    evidence_key = cfg.get("evidence_field", "evidence")
    system_prompt = build_system_prompt(schema, cfg["task"], cfg.get("instructions", ""), evidence_key)
    enrich = domains.load(cfg.get("domain"))
    paper_flags = domains.load_paper_flags(cfg.get("domain"))
    rep_cfg = cfg.get("repair") or {}
    rep_rules = {**GENERIC, **(domains.load_hook(cfg.get("domain"), "REPAIRS", {}) or {})}
    context_fn = domains.load_hook(cfg.get("domain"), "paper_context", lambda text: "")
    papers = {}                     # source_file -> (full text, context for repairs)
    pf = cfg["prefilter"]
    groups = cfg.get("paper_groups") or {}

    records, files, lookups = [], [], {}
    with log.stage("gather"):
        sources = gather_sources(cfg, root)
    print(f"{len(sources)} file(s) to process")

    for path, meta in sources:
        try:
            with log.stage("parse", file=path.name):
                doc = parse_document(path, pf.get("chunk_chars", 2500), pf.get("table_rows_per_chunk"))
        except Exception as exc:
            print(f"- {path.name}: [skip] could not read ({exc})")
            files.append({"file": path.name, "status": f"unreadable: {exc}"})
            continue
        doc.title = meta.get("title") or doc.title
        doc.doi = meta.get("doi") or doc.doi
        group = group_of(path.name, groups)
        full_text = " ".join(c.text for c in doc.chunks)
        flags = paper_flags(full_text)
        papers[path.name] = (full_text, context_fn(full_text) if rep_cfg.get("enabled") else "")
        found_labels = label_lookup(doc.chunks, path.name)
        lookups.setdefault(group, {}).update({k: v for k, v in found_labels.items()
                                              if k not in lookups.get(group, {})})
        chosen = select(doc.chunks, pf)
        summary = {"file": path.name, "paper_group": group, "title": doc.title, "doi": doc.doi,
                   "source": meta["source"], "chunks_total": len(doc.chunks), "chunks_sent": len(chosen),
                   "sequence_labels_found": len(found_labels), "records": 0, "status": "ok"}
        print(f"- {path.name}: {len(doc.chunks)} chunks, {len(chosen)} selected"
              + (f", {len(found_labels)} label->sequence entries" if found_labels else ""))

        if dry_run:
            for c in chosen:
                print(f"    [{c.score:>2}] {c.label}: {c.text[:100].replace(chr(10), ' ')}")
            files.append(summary)
            continue

        stop = False
        for c in chosen:
            log.context = {"file": path.name, "location": c.label}
            try:
                with log.stage("extract", file=path.name, location=c.label):
                    found = extract(c.text, schema, model, system_prompt, doc.title, c.label, c.kind,
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
            for rec in found:
                rec["_problems"] = rec.get("_problems", []) + _schema_problems(rec, schema)
                rec.update(doi=doc.doi, paper_title=doc.title, source=meta["source"],
                           source_file=path.name, page=c.page, location=c.label, _group=group, _passage=c.text,
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
            log.context = {"file": rec.get("source_file"), "location": rec.get("location")}
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
