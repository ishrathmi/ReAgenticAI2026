"""Download open-access PDFs found by the search tool. Files are cached under the target folder."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

HEADERS = {"User-Agent": "litagent/0.1 (ReAgenticAI2026 academic workshop)"}


def fetch(item: dict, dest_dir: str = ".cache/papers") -> Path | None:
    """Download item['pdf_url'] to dest_dir. Returns the local path, or None on failure."""
    url = item.get("pdf_url")
    if not url:
        print(f"  [skip] no open-access PDF: {item.get('title', '')[:70]}")
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", item.get("doi") or str(item.get("id")))[:80]
    path = dest / f"{item.get('source', 'paper').lower()}_{stem}.pdf"
    if path.exists():
        return path
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=120, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"  [skip] download error: {exc}")
        return None
    if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
        print(f"  [skip] download failed ({resp.status_code}): {item.get('title', '')[:70]}")
        return None
    path.write_bytes(resp.content)
    return path
