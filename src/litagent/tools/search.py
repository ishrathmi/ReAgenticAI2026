"""Resolve search queries to paper metadata.

Use official APIs only: Crossref, OpenAlex, Semantic Scholar, arXiv, ChemRxiv, Europe PMC.
Do not scrape publisher websites; most chemistry publishers prohibit automated downloading.

Implemented: ChemRxiv (public Engage API, no key). Add other sources as functions with the
same return shape: a list of {"source", "id", "title", "doi", "published", "pdf_url"}.
"""

from __future__ import annotations

import httpx

CHEMRXIV_API = "https://chemrxiv.org/engage/chemrxiv/public-api/v1/items"
HEADERS = {"User-Agent": "litagent/0.1 (ReAgenticAI2026 academic workshop)"}


def search(query: str, source: str = "chemrxiv", limit: int = 10) -> list:
    if source == "chemrxiv":
        return search_chemrxiv(query, limit)
    raise NotImplementedError(f"Search source '{source}' is not implemented yet")


def _chemrxiv_pdf_url(item: dict) -> str | None:
    original = (item.get("asset") or {}).get("original") or {}
    if original.get("url"):
        return original["url"]
    for a in item.get("assets") or []:
        url = (a.get("original") or {}).get("url") or a.get("url")
        if url and ".pdf" in url.lower():
            return url
    return None


def search_chemrxiv(query: str, limit: int = 10) -> list:
    resp = httpx.get(CHEMRXIV_API, params={"term": query, "limit": limit},
                     headers=HEADERS, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    hits = data.get("itemHits") or data.get("items") or []
    results = []
    for h in hits:
        item = h.get("item", h)
        results.append({
            "source": "ChemRxiv",
            "id": item.get("id"),
            "title": item.get("title", ""),
            "doi": item.get("doi", ""),
            "published": item.get("publishedDate", ""),
            "pdf_url": _chemrxiv_pdf_url(item),
        })
    return results
