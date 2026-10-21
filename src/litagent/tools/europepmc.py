"""Europe PMC: search open-access papers, download full text (JATS XML) and supplementary files.

REST API, no key needed: https://europepmc.org/RestfulWebService
Only the open-access subset has full text and SI files; other results are skipped.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import httpx

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
HEADERS = {"User-Agent": "litagent/0.1 (ReAgenticAI2026 academic workshop)"}
SI_TYPES = {".pdf", ".xlsx", ".docx"}


def search(query: str, limit: int = 10) -> list:
    """Open-access papers with full text in Europe PMC matching the query."""
    q = f"({query}) AND OPEN_ACCESS:y AND HAS_FT:y"
    resp = httpx.get(f"{BASE}/search", params={"query": q, "format": "json", "resultType": "core",
                                               "pageSize": limit}, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    out = []
    for r in resp.json().get("resultList", {}).get("result", []):
        pmcid = r.get("pmcid")
        if not pmcid:
            continue
        out.append({
            "source": "Europe PMC", "id": pmcid, "pmcid": pmcid, "title": r.get("title", ""),
            "doi": r.get("doi", ""), "published": r.get("pubYear", ""), "authors": r.get("authorString", ""),
            "license": r.get("license", ""), "has_si": r.get("hasSuppl") == "Y", "pdf_url": None,
        })
    return out


def fetch_fulltext(pmcid: str, dest_dir: str) -> Path | None:
    """Download the JATS XML full text to dest_dir/<PMCID>.xml (cached)."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{pmcid}.xml"
    if path.exists():
        return path
    resp = httpx.get(f"{BASE}/{pmcid}/fullTextXML", headers=HEADERS, timeout=120)
    if resp.status_code != 200 or b"<article" not in resp.content[:5000]:
        print(f"  [skip] no full text for {pmcid} ({resp.status_code})")
        return None
    path.write_bytes(resp.content)
    return path


def fetch_supplementary(pmcid: str, dest_dir: str) -> list:
    """Download and unpack the SI files (PDF, XLSX, DOCX) to dest_dir/<PMCID>_si/ (cached)."""
    folder = Path(dest_dir) / f"{pmcid}_si"
    if folder.exists():
        return sorted(p for p in folder.iterdir() if p.suffix.lower() in SI_TYPES)
    resp = httpx.get(f"{BASE}/{pmcid}/supplementaryFiles", headers=HEADERS, timeout=180, follow_redirects=True)
    if resp.status_code != 200 or not resp.content.startswith(b"PK"):
        return []
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if Path(name).suffix.lower() in SI_TYPES and not name.startswith("."):
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
                target = folder / f"{pmcid}_si_{safe}"
                target.write_bytes(z.read(info))
                saved.append(target)
    return saved
