#!/usr/bin/env python3
"""Download the PDFs of papers filed in vaults and zip them, one folder per vault.

Used by serve.py (POST /api/download). PDFs are fetched only from biorxiv.org / medrxiv.org and
cached in digests/_cache/pdfs/ so a second download is instant. A paper whose PDF can't be
fetched gets a .url shortcut instead, and is listed in the index.

Zip layout:
    <topic>_<date>/
        README.md            every vault with title, authors, score, Claude's reason, your note, link
        references.bib       BibTeX for everything in the zip (vault name in `keywords`)
        <Vault name>/<First author> <year> - <title>.pdf
"""
import io, os, re, time, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PAPER_URL = re.compile(r"^https://(www\.(biorxiv|medrxiv)\.org/content/10\.\d{4,9}/[\w.\-]+v\d+|chemrxiv\.org/doi/(full|pdf)/10\.26434/[\w.\-]+/v\d+)$")
CHEMRXIV_NOTE = "ChemRxiv blocks automated downloads; open the link in your browser"


def safe(name, limit=110):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:limit].rsplit(" ", 1)[0] if len(name) > limit else name) or "untitled"


def first_author(authors):
    a = (authors or "").split(";")[0].strip()
    return a.split(",")[0].strip() or "Anon"


def fetch_pdf(url, cache_dir):
    """Return PDF bytes for a bioRxiv/medRxiv paper URL, or raise."""
    if not PAPER_URL.match(url or ""):
        raise ValueError("not a bioRxiv, medRxiv or ChemRxiv paper link")
    if "chemrxiv.org" in url:
        raise ValueError(CHEMRXIV_NOTE)
    key = re.sub(r"[^\w.\-]+", "_", url.split("/content/", 1)[1]) + ".pdf"
    path = os.path.join(cache_dir, key)
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return open(path, "rb").read()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url + ".full.pdf", headers={"User-Agent": UA, "Accept": "application/pdf"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if not data.startswith(b"%PDF"):
                raise ValueError("the server did not return a PDF")
            os.makedirs(cache_dir, exist_ok=True)
            open(path, "wb").write(data)
            return data
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def bibtex(p, vault):
    year = (p.get("date") or "")[:4]
    key = re.sub(r"[^A-Za-z]", "", first_author(p.get("authors"))) + year + "_" + p["id"].split(".")[-1]
    authors = " and ".join(a.strip() for a in (p.get("authors") or "").split(";") if a.strip())
    journal = {"medrxiv": "medRxiv", "chemrxiv": "ChemRxiv"}.get(p.get("source", ""), "bioRxiv")
    tags = ", ".join([vault] + list(p.get("tags") or []))
    return (f"@article{{{key},\n  title = {{{p.get('title', '')}}},\n  author = {{{authors}}},\n  journal = {{{journal}}},\n"
            f"  year = {{{year}}},\n  doi = {{{p['id']}}},\n  url = {{{p.get('url', '')}}},\n  keywords = {{{tags}}}\n}}")


def build_zip(job, topic, end, vaults, cache_dir):
    """vaults: [{"name": str, "papers": [{id,title,authors,date,url,score,reason,tags,note}]}].
    Updates job (dict) with progress; returns zip bytes."""
    root = safe(f"{topic or 'Literature Vault'} {end}".strip(), 80)
    items = [(v["name"], p) for v in vaults for p in v["papers"]]
    job.update(total=len(items), done=0, ok=0, failed=[])

    def one(item):
        vault, p = item
        try:
            data, err = fetch_pdf(p.get("url", ""), cache_dir), None
        except Exception as e:
            data, err = None, str(e)
        job["done"] += 1
        return vault, p, data, err

    buf = io.BytesIO()
    readme = [f"# {topic}", "", f"Exported from Literature Vault · {end}", ""]
    bib = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, ThreadPoolExecutor(3) as ex:
        results = list(ex.map(one, items))
        used = set()
        for v in vaults:
            if not v["papers"]:
                continue
            folder = safe(v["name"], 60)
            readme += [f"## {v['name']} ({len(v['papers'])})", ""]
            for vault, p, data, err in [r for r in results if r[0] == v["name"]]:
                base = safe(f"{first_author(p.get('authors'))} {(p.get('date') or '')[:4]} - {p.get('title', '')}")
                name, n = base, 2
                while f"{folder}/{name}" in used:
                    name, n = f"{base} ({n})", n + 1
                used.add(f"{folder}/{name}")
                if data:
                    z.writestr(f"{root}/{folder}/{name}.pdf", data)
                    job["ok"] += 1
                    status = f"[{name}.pdf]({folder.replace(' ', '%20')}/{(name + '.pdf').replace(' ', '%20')})"
                else:
                    z.writestr(f"{root}/{folder}/{name}.url", f"[InternetShortcut]\nURL={p.get('url', '')}\n")
                    job["failed"].append({"title": p.get("title", ""), "error": err})
                    status = f"PDF not downloaded ({err}). Open the link."
                readme.append(f"- **{p.get('title', '')}** ({p.get('score', '?')}/100)  ")
                readme.append(f"  {p.get('authors', '')} · {p.get('date', '')} · [{p['id']}]({p.get('url', '')})  ")
                if p.get("reason"):
                    readme.append(f"  _Why:_ {p['reason']}  ")
                if p.get("note"):
                    readme.append(f"  _Your note:_ {p['note']}  ")
                readme += [f"  {status}", ""]
                bib.append(bibtex(p, v["name"]))
        z.writestr(f"{root}/README.md", "\n".join(readme) + "\n")
        z.writestr(f"{root}/references.bib", "\n\n".join(bib) + "\n")
    job["filename"] = root + ".zip"
    return buf.getvalue()
