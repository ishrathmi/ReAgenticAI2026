"""Resolve DOIs or search queries to paper metadata.

Use official APIs only: Crossref, OpenAlex, Semantic Scholar, arXiv, ChemRxiv, Europe PMC.
Do not scrape publisher websites; most chemistry publishers prohibit automated downloading.
"""


def search(query: str) -> list[dict]:
    raise NotImplementedError
