"""DOI helpers: one spelling per DOI, so the ledger never sees the same paper twice."""
import re

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)
_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")


def clean_doi(doi):
    """Lower-case bare DOI ("10.1002/adma.201602776"); DOIs are case-insensitive."""
    d = doi.strip()
    for p in _PREFIXES:
        if d.lower().startswith(p):
            d = d[len(p):]
    return d.strip().rstrip(".,;)]").lower()


def doi_slug(doi):
    """File-name-safe form: "10.1002/adma.201602776" -> "10.1002_adma.201602776"."""
    return re.sub(r'[/\\:*?"<>|]', "_", clean_doi(doi))


def find_dois(text):
    """All DOIs in a text, cleaned, in order of first appearance."""
    return list(dict.fromkeys(clean_doi(m) for m in DOI_RE.findall(text)))
