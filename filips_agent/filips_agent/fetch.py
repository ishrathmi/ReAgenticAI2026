"""Paper metadata from OpenAlex, and full texts from open access or the inbox (RULES.md W6).

Set OPENALEX_API_KEY (free key from openalex.org, higher daily limit) or at least
OPENALEX_EMAIL. Responses are cached in data/cache/openalex/, so a paper costs API
calls only once.

Full texts live in data/pdfs/ as <doi slug>.pdf, with SI as <doi slug>_SI<n>.<ext>.
PDFs you download yourself go in data/inbox/ under any name; collect_inbox() finds
their DOI and files them.
"""
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .dois import DOI_RE, clean_doi, doi_slug, find_dois
from .ledger import AWAITING_PDF, AWAITING_SI, DATA_DIR, TO_READ, WISHED

OPENALEX = "https://api.openalex.org"
CACHE_DIR = DATA_DIR / "cache" / "openalex"
INBOX = DATA_DIR / "inbox"
PDF_DIR = DATA_DIR / "pdfs"
SI_EXTS = (".pdf", ".docx", ".doc", ".txt")  # P8a: documents only
_BATCH = 50  # values per OpenAlex OR-filter


# --- HTTP -----------------------------------------------------------------------

def _get(url, tries=3, data=None):
    """GET, or POST when data (bytes) is given; retries on rate limits and server errors."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "filips_agent/0.1"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except urllib.error.URLError:
            if attempt == tries - 1:
                raise
        time.sleep(2 ** attempt)


def _openalex(path, **params):
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    elif os.environ.get("OPENALEX_EMAIL"):
        params["mailto"] = os.environ["OPENALEX_EMAIL"]
    query = urllib.parse.urlencode(params, safe=":|,/")
    return json.loads(_get(f"{OPENALEX}{path}" + (f"?{query}" if query else "")))


# --- OpenAlex -----------------------------------------------------------------

def abstract_from_index(index):
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return None
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    return " ".join(word for _, word in words)


def _work(raw):
    source = (raw.get("primary_location") or {}).get("source") or {}
    best = raw.get("best_oa_location") or {}
    return {
        "doi": clean_doi(raw["doi"]) if raw.get("doi") else None,
        "openalex_id": raw["id"].rsplit("/", 1)[-1],
        "title": raw.get("title") or raw.get("display_name"),
        "abstract": abstract_from_index(raw.get("abstract_inverted_index")),
        "year": raw.get("publication_year"),
        "journal": source.get("display_name"),
        "is_oa": (raw.get("open_access") or {}).get("is_oa", False),
        "pdf_url": best.get("pdf_url"),
        "referenced_works": [w.rsplit("/", 1)[-1] for w in raw.get("referenced_works") or []],
    }


def _cache_path(doi, cache_dir):
    return Path(cache_dir) / f"{doi_slug(doi)}.json"


def _save(work, cache_dir):
    path = _cache_path(work["doi"], cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(work, indent=1), encoding="utf-8")


def get_works(dois, cache_dir=CACHE_DIR):
    """{doi: work} for every DOI OpenAlex knows; cached ones cost no API call.

    Batches of 50, so triaging a paper's references takes one call per 50 abstracts.
    """
    wanted = list(dict.fromkeys(clean_doi(d) for d in dois))
    found, missing = {}, []
    for d in wanted:
        path = _cache_path(d, cache_dir)
        if path.exists():
            found[d] = json.loads(path.read_text(encoding="utf-8"))
        else:
            missing.append(d)
    for i in range(0, len(missing), _BATCH):
        chunk = missing[i:i + _BATCH]
        data = _openalex("/works", filter="doi:" + "|".join(chunk), **{"per-page": _BATCH})
        for raw in data.get("results", []):
            work = _work(raw)
            if work["doi"] in chunk:
                _save(work, cache_dir)
                found[work["doi"]] = work
    return found


def get_work(doi, cache_dir=CACHE_DIR):
    """One paper's metadata, or None if OpenAlex doesn't know the DOI."""
    return get_works([doi], cache_dir).get(clean_doi(doi))


def reference_list(work, cache_dir=CACHE_DIR):
    """[{"doi", "title"}] for the works a paper cites (references without a DOI are dropped)."""
    if "references" not in work:
        ids, found = work["referenced_works"], {}
        for i in range(0, len(ids), _BATCH):
            chunk = ids[i:i + _BATCH]
            data = _openalex("/works", filter="openalex:" + "|".join(chunk),
                             select="id,doi,title", **{"per-page": _BATCH})
            for raw in data.get("results", []):
                if raw.get("doi"):
                    found[raw["id"].rsplit("/", 1)[-1]] = {"doi": clean_doi(raw["doi"]), "title": raw.get("title")}
        work["references"] = [found[i] for i in ids if i in found]  # keep OpenAlex order
        work.pop("reference_dois", None)  # older cache format
        if work.get("doi"):
            _save(work, cache_dir)
    return work["references"]


def reference_dois(work, cache_dir=CACHE_DIR):
    """DOIs of the works a paper cites."""
    return [r["doi"] for r in reference_list(work, cache_dir)]


# --- full text ------------------------------------------------------------------

def get_fulltext(doi, pdf_dir=PDF_DIR):
    """{"main": Path, "si": [Path]} if the paper's PDF is in data/pdfs/, else None."""
    main = Path(pdf_dir) / f"{doi_slug(doi)}.pdf"
    if not main.exists():
        return None
    return {"main": main, "si": si_files(doi, pdf_dir=pdf_dir)}


def si_files(doi, pdf_dir=PDF_DIR):
    """The paper's filed SI files, also when its main text hasn't arrived yet."""
    return sorted(p for p in Path(pdf_dir).glob(f"{doi_slug(doi)}_SI*") if p.suffix.lower() in SI_EXTS)


def download_oa_pdf(work, pdf_dir=PDF_DIR):
    """Try the open-access PDF link. Returns the saved path, or None if it isn't a PDF."""
    if not work.get("pdf_url"):
        return None
    try:
        data = _get(work["pdf_url"], tries=1)
    except (urllib.error.URLError, TimeoutError):
        return None
    if not data.startswith(b"%PDF"):  # landing pages and bot checks come back as HTML
        return None
    path = Path(pdf_dir) / f"{doi_slug(work['doi'])}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def acquire(ledger, doi, pdf_dir=PDF_DIR, cache_dir=CACHE_DIR):
    """Make sure a reading-list paper has its full text; otherwise wish-list it (W6).

    Returns the get_fulltext() result, or None if the paper now awaits a PDF.
    """
    text = get_fulltext(doi, pdf_dir)
    if text:
        return text
    work = get_work(doi, cache_dir)
    if work and download_oa_pdf(work, pdf_dir):
        return get_fulltext(doi, pdf_dir)
    reason = "no open-access PDF found" if work else "DOI not found in OpenAlex"
    ledger.set_status(doi, AWAITING_PDF, reason, title=work["title"] if work else None)
    return None


# --- inbox ------------------------------------------------------------------------

def _first_pages_text(path, pages=2):
    import pymupdf
    with pymupdf.open(path) as doc:
        return "\n".join(doc[i].get_text() for i in range(min(pages, len(doc))))


def _looks_like_si(path, text):
    words = re.split(r"[^a-z0-9]+", path.stem.lower())
    # Wiley: "-sup-0001"; Springer Nature: "_MOESM1_ESM"
    if any(w in ("si", "esi", "sup", "esm") or w.startswith(("supp", "moesm")) for w in words):
        return True
    head = text[:500].lower()
    return any(k in head for k in ("supporting information", "supplementary information",
                                   "electronic supplementary", "supplementary material"))


# Springer Nature ESM files: 40843_2020_1639_MOESM1_ESM.pdf belongs to 10.1007/s40843-020-1639-x
_SPRINGER_ESM = re.compile(r"^(\d{5})_(\d{4})_(\d+)_moesm\d+_esm$")


def _rule_doi(path, text, wanted):
    """The wanted DOI this file belongs to by file name or printed DOI, or None."""
    stem = path.stem.lower()
    for slug, doi in wanted.items():
        if stem == slug or stem.startswith(slug + "_si"):
            return doi
    m = _SPRINGER_ESM.match(stem)
    if m:
        prefix = f"s{m[1]}-{m[2][1:]}-{int(m[3]):04d}-"
        hit = next((d for d in wanted.values() if d.split("/", 1)[1].startswith(prefix)), None)
        if hit:
            return hit
    return next((d for d in find_dois(text) if doi_slug(d) in wanted), None)


def _rule_kind(path, text, doi):
    stem = path.stem.lower()
    if stem == doi_slug(doi):
        return "main"
    return "si" if stem.startswith(doi_slug(doi) + "_si") or _looks_like_si(path, text) else "main"


def _file_text(path):
    if path.suffix.lower() == ".pdf":
        return _first_pages_text(path)
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")[:8000]
    return ""


def _title_on_page(title, text, share=0.8):
    """Are most of the title's words (longer than 3 letters) printed on the page?"""
    words = {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3}
    page = set(re.findall(r"[a-z0-9]+", text.lower()))
    return bool(words) and len(words & page) >= share * len(words)


def identify_dropped(ledger, inbox=INBOX, cache_dir=CACHE_DIR):
    """Find which paper each main-text PDF in the inbox is; put new papers on the reading list.

    A PDF's DOI is taken from its first page and only trusted when OpenAlex gives that DOI a
    title that is printed on the page too, because a first page can also cite other DOIs.
    SI files are left for collect_inbox, which files them once their paper is wanted.
    Returns {file name: (doi, added)}; added is False for a paper the ledger already knew.
    """
    found = {}
    for path in sorted(Path(inbox).glob("*")):
        if path.suffix.lower() != ".pdf":
            continue
        try:
            text = _first_pages_text(path, pages=1)
        except Exception:  # damaged; collect_inbox says so
            continue
        if _looks_like_si(path, text):
            continue
        for doi in find_dois(text)[:5]:
            work = get_work(doi, cache_dir)
            if not (work and work["title"] and _title_on_page(work["title"], text)):
                continue
            found[path.name] = (doi, ledger.add(doi, TO_READ, title=work["title"], reason="dropped in"))
            break
    return found


def assign_doi(ledger, name, doi, inbox=INBOX, cache_dir=CACHE_DIR):
    """For a dropped file whose DOI couldn't be found: put the paper on the reading list under
    the DOI given and rename the file so collect_inbox files it as that paper's main text, or
    as its SI when the file looks like supporting information. Returns the new path.
    """
    path = Path(inbox) / name
    if not path.is_file():
        raise FileNotFoundError(f"{name} is not in the inbox")
    doi = clean_doi(doi)
    if not DOI_RE.fullmatch(doi):
        raise ValueError(f"{doi!r} is not a DOI")
    known = ledger.get(doi)
    if known and known["status"] not in (TO_READ, *WISHED):
        raise ValueError(f"{doi} is already in the ledger as {known['status']}")
    if not known:
        work = get_work(doi, cache_dir)
        ledger.add(doi, TO_READ, title=work["title"] if work else None, reason="dropped in")
    si = path.suffix.lower() != ".pdf" or _looks_like_si(path, _file_text(path))
    dest = path.with_name(doi_slug(doi) + (f"_SI_{path.stem}" if si else "") + path.suffix.lower())
    path.rename(dest)
    return dest


def collect_inbox(ledger, inbox=INBOX, pdf_dir=PDF_DIR, sorter=None, leave=None):
    """File documents from the inbox under their DOI; waiting papers go back on the reading list.

    The rules match a file by its name (<doi slug>.pdf, <doi slug>_SI.pdf, Springer Nature
    ESM names) or by a wanted DOI printed on its first pages, and tell main text from SI by
    keywords. sorter (agents.inbox_sorter.sort_file, or None for rules only) gives a second
    opinion; a file is only filed when rules and sorter agree on the kind, and (when both
    found one) on the paper.

    leave: {file name: why} of files to leave in the inbox without looking at them.
    Returns (filed, skipped): {doi: [paths]} and {file name: why it stayed in the inbox}.
    """
    Path(inbox).mkdir(parents=True, exist_ok=True)
    sources = [s for st in (*WISHED, TO_READ) for s in ledger.queue(st)]
    wanted = {doi_slug(s["doi"]): s["doi"] for s in sources}
    titles = [{"doi": s["doi"], "title": s["title"]} for s in sources]
    plan, skipped = [], {}

    for path in sorted(Path(inbox).glob("*")):
        if not path.is_file():
            continue
        if leave and path.name in leave:
            skipped[path.name] = leave[path.name]
            continue
        if path.suffix.lower() not in SI_EXTS:
            skipped[path.name] = "not a document type that is read (P8a)"
            continue
        try:
            text = _file_text(path)
        except Exception:
            skipped[path.name] = "can't open this file: it may be damaged, or not really a PDF"
            continue
        doi = _rule_doi(path, text, wanted)
        kind = _rule_kind(path, text, doi) if doi else None
        if sorter is not None and wanted:
            opinion = sorter(path.name, text, titles)
            if opinion["kind"] == "other":
                skipped[path.name] = f"sorter: not a wanted paper or its SI ({opinion['reason']})"
                continue
            if doi and opinion["doi"] and opinion["doi"] != doi:
                skipped[path.name] = f"rules say {doi}, sorter says {opinion['doi']}"
                continue
            if doi and opinion["kind"] != kind:
                skipped[path.name] = f"rules say {kind}, sorter says {opinion['kind']} ({opinion['reason']})"
                continue
            if not doi and opinion["doi"]:
                doi, kind = opinion["doi"], opinion["kind"]
        if not doi:
            skipped[path.name] = ("looks like supporting information, but its paper isn't on the reading list: "
                                  "drop the paper too, or type the paper's DOI"
                                  if _looks_like_si(path, text) else
                                  "no matching paper on the reading list or wish list")
            continue
        if kind == "main" and path.suffix.lower() != ".pdf":
            skipped[path.name] = "main text must be a PDF"
            continue
        plan.append((path, doi, kind))

    # main texts first, so an SI can never take the main slot
    mains = {}
    for path, doi, kind in plan:
        if kind == "main":
            mains.setdefault(doi, []).append(path)
    filed = {}
    Path(pdf_dir).mkdir(parents=True, exist_ok=True)
    for doi, paths in mains.items():
        dest = Path(pdf_dir) / f"{doi_slug(doi)}.pdf"
        if len(paths) > 1:
            for p in paths:
                skipped[p.name] = f"{len(paths)} files look like the main text of {doi}"
        elif dest.exists():
            skipped[paths[0].name] = f"already have the main text of {doi}"
        else:
            shutil.move(str(paths[0]), dest)
            filed.setdefault(doi, []).append(dest)
    for path, doi, kind in plan:
        if kind == "si":
            slug = doi_slug(doi)
            n = len(list(Path(pdf_dir).glob(f"{slug}_SI*"))) + 1
            dest = Path(pdf_dir) / f"{slug}_SI{n}{path.suffix.lower()}"
            shutil.move(str(path), dest)
            filed.setdefault(doi, []).append(dest)

    for doi in filed:
        status = ledger.get(doi)["status"]
        if status == AWAITING_PDF and get_fulltext(doi, pdf_dir):
            ledger.set_status(doi, TO_READ, "PDF provided")
        elif status == AWAITING_SI and si_files(doi, pdf_dir=pdf_dir):  # read again, now with its SI
            ledger.set_status(doi, TO_READ, "SI provided")
    return filed, skipped
