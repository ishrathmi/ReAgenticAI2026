#!/usr/bin/env python3
"""Preprint databases LitVault can screen, behind one function: fetch(source, start, end).

Every source returns papers in the same shape (bioRxiv's field names, plus a few extras):
    doi, title, authors ("Last, F.; Last, F."), author_corresponding_institution, date (YYYY-MM-DD),
    version, category, abstract, source, url (landing page), pdf_url

  biorxiv, medrxiv  api.biorxiv.org (official API)
  chemrxiv          api.crossref.org, DOI prefix 10.26434. ChemRxiv's own API and pages sit behind
                    a Cloudflare bot check that scripts can't pass, so PDFs can't be downloaded
                    automatically; the landing link opens fine in a browser.
"""
import html, json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES = {"biorxiv": "bioRxiv", "medrxiv": "medRxiv", "chemrxiv": "ChemRxiv"}
# Crossref asks API users to identify themselves; set LITVAULT_CONTACT to your email for its faster "polite" pool
CONTACT = os.environ.get("LITVAULT_CONTACT", "litvault-local")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def parse_sources(value):
    """'biorxiv, chemrxiv' or ['bioRxiv'] -> ['biorxiv', 'chemrxiv'] (unknown names dropped, default biorxiv)."""
    items = value if isinstance(value, list) else re.split(r"[,;\s]+", value or "")
    out = [s.strip().lower() for s in items if s.strip().lower() in SOURCES]
    return list(dict.fromkeys(out)) or ["biorxiv"]


def get_json(url, headers=None, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


# ---- bioRxiv / medRxiv ----------------------------------------------------------------------
def fetch_rxiv(server, start, end, workers=6, progress=None):
    api = "https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}"
    fmt = dict(server=server, start=start.isoformat(), end=end.isoformat())
    first = get_json(api.format(cursor=0, **fmt))
    msg = first["messages"][0]
    total, page = int(msg.get("total", 0)), int(msg.get("count", 0)) or 30
    cursors = list(range(page, total, page))
    papers = list(first.get("collection", []))
    with ThreadPoolExecutor(workers) as ex:
        for i, res in enumerate(ex.map(lambda c: get_json(api.format(cursor=c, **fmt)), cursors), 2):
            papers.extend(res.get("collection", []))
            if progress:
                progress(i, len(cursors) + 1)
    latest = {}  # keep the latest version of each DOI
    for p in papers:
        d = p["doi"]
        if d not in latest or int(p.get("version", 1)) > int(latest[d].get("version", 1)):
            latest[d] = p
    out = []
    for p in latest.values():
        url = f"https://www.{server}.org/content/{p['doi']}v{p.get('version', 1)}"
        out.append({**p, "source": server, "url": url, "pdf_url": url + ".full.pdf"})
    return out


# ---- ChemRxiv via Crossref ------------------------------------------------------------------
def clean_jats(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_chemrxiv(start, end, progress=None, contact=None):
    base = "https://api.crossref.org/prefixes/10.26434/works"
    params = {"filter": f"from-posted-date:{start.isoformat()},until-posted-date:{end.isoformat()}",
              "rows": "500", "select": "DOI,title,abstract,author,posted,resource,link"}
    headers = {"User-Agent": f"LitVault/1.0 (mailto:{contact or CONTACT})"}
    cursor, items, total, page = "*", [], None, 0
    while True:
        q = urllib.parse.urlencode({**params, "cursor": cursor})
        msg = get_json(f"{base}?{q}", headers)["message"]
        total = total if total is not None else msg.get("total-results", 0)
        batch = msg.get("items", [])
        items += batch
        page += 1
        if progress:
            progress(min(len(items), total), max(total, 1))
        cursor = msg.get("next-cursor")
        if not batch or not cursor or len(items) >= total:
            break
    latest = {}
    for it in items:
        doi = it["DOI"]
        m = re.match(r"^(.*?)(?:/v(\d+))?$", doi)
        stem, ver = m.group(1), int(m.group(2) or 1)
        if stem not in latest or ver > latest[stem][0]:
            latest[stem] = (ver, it)
    out = []
    for stem, (ver, it) in latest.items():
        dp = (it.get("posted") or {}).get("date-parts", [[None]])[0]
        date = "-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(dp)) if dp and dp[0] else ""
        authors = "; ".join(f"{a.get('family', '')}, {' '.join(w[0] + '.' for w in (a.get('given') or '').split() if w)}".strip(", ")
                            for a in it.get("author", []) if a.get("family"))
        affil = next((x.get("name") for a in it.get("author", [])[:1] for x in a.get("affiliation", []) if x.get("name")), "")
        url = (it.get("resource") or {}).get("primary", {}).get("URL") or f"https://chemrxiv.org/doi/full/{it['DOI']}"
        pdf = next((l["URL"] for l in it.get("link", []) if "/pdf/" in l.get("URL", "")), "")
        out.append({"doi": it["DOI"], "title": clean_jats((it.get("title") or [""])[0]), "authors": authors,
                    "author_corresponding_institution": affil, "date": date, "version": str(ver),
                    "category": "chemistry", "abstract": clean_jats(it.get("abstract")),
                    "source": "chemrxiv", "url": url, "pdf_url": pdf})
    return out


def fetch(source, start, end, progress=None):
    """All preprints posted start..end (date objects) in one source, in the common shape."""
    print(f"fetching {source} {start}..{end}", file=sys.stderr)
    if source in ("biorxiv", "medrxiv"):
        return fetch_rxiv(source, start, end, progress=progress)
    if source == "chemrxiv":
        return fetch_chemrxiv(start, end, progress=progress)
    raise ValueError(f"unknown source: {source}")
