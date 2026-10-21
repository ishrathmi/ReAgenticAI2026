"""Read JATS XML (the full-text format of Europe PMC and PubMed Central) into chunks.

Structured XML is more reliable than PDF: section titles, paragraphs and table cells are marked
explicitly, so chunks get exact section labels and tables keep their rows and captions.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el) -> str:
    """All text inside an element, with whitespace collapsed; references in [brackets] kept short."""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _find(el, path: str):
    """First descendant matching a '/'-separated path of local names."""
    nodes = [el]
    for part in path.split("/"):
        nodes = [c for n in nodes for c in n.iter() if _local(c.tag) == part and c is not n]
        if not nodes:
            return None
    return nodes[0]


def _table_text(wrap) -> tuple:
    label = _text(_find(wrap, "label")) if _find(wrap, "label") is not None else ""
    cap_el = _find(wrap, "caption")
    caption = _text(cap_el) if cap_el is not None else ""
    rows = []
    for tr in (n for n in wrap.iter() if _local(n.tag) == "tr"):
        cells = [_text(c) for c in tr if _local(c.tag) in ("td", "th")]
        if any(cells):
            rows.append(" | ".join(cells))
    foot = _find(wrap, "table-wrap-foot")
    return label, caption, "\n".join(rows), (_text(foot) if foot is not None else "")


def read_jats(doc, chunk_chars: int, split, split_table) -> None:
    """Fill doc (a parse_pdf.Document) with chunks from the JATS file at doc.path."""
    from .parse_pdf import Chunk

    root = ET.parse(doc.path).getroot()
    name = doc.path.name
    title = _find(root, "article-meta/title-group/article-title")
    doc.title = _text(title) if title is not None else ""
    for aid in (n for n in root.iter() if _local(n.tag) == "article-id"):
        if aid.get("pub-id-type") == "doi":
            doc.doi = (aid.text or "").strip()

    def add_text(section, paragraphs):
        body = "\n\n".join(p for p in paragraphs if p)
        for j, piece in enumerate(split(body, chunk_chars), start=1):
            doc.chunks.append(Chunk(name, None, "text", f"{section[:40]} text {j}", piece, section=section))

    def add_table(section, wrap, n):
        label, caption, rows, foot = _table_text(wrap)
        if not rows:
            return
        head = " ".join(x for x in (label, caption) if x)
        for k, piece in enumerate(split_table(rows, chunk_chars), start=1):
            body = (f"{head}\n" if head else "") + piece + (f"\n[table footnote: {foot}]" if foot else "")
            lab = (label or f"table {n}") + (f" part {k}" if k > 1 else "")
            doc.chunks.append(Chunk(name, None, "table", lab, body, section=section))

    abstract = _find(root, "article-meta/abstract")
    if abstract is not None:
        add_text("Abstract", [_text(p) for p in abstract.iter() if _local(p.tag) == "p"])

    n_table = [0]

    def walk(sec, parent):
        """parent: section label of the enclosing section ('' at top level); subsections inherit it."""
        from .parse_pdf import _section_name
        t = _children(sec, "title")
        heading = _text(t[0]) if t else ""
        if parent == "Methods" or sec.get("sec-type", "").startswith(("method", "materials")) \
                or re.search(r"\bmethods?\b|experimental|materials and", heading, re.I):
            section = "Methods"
        else:
            section = parent or (_section_name(heading) if heading else "Body")
        paras = []
        for child in sec:
            tag = _local(child.tag)
            if tag == "p":
                for wrap in [n for n in child.iter() if _local(n.tag) == "table-wrap"]:
                    n_table[0] += 1
                    add_table(section, wrap, n_table[0])
                paras.append(_text(child))
            elif tag == "table-wrap":
                n_table[0] += 1
                add_table(section, child, n_table[0])
            elif tag == "sec":
                add_text(section, paras)
                paras = []
                walk(child, section)
        add_text(section, paras)

    body = _find(root, "body")
    if body is not None:
        loose = [_text(p) for p in _children(body, "p")]
        add_text("Body", loose)
        for sec in _children(body, "sec"):
            walk(sec, "")
    back = _find(root, "back")
    if back is not None:                       # tables or methods placed in the back matter
        for wrap in (n for n in back.iter() if _local(n.tag) == "table-wrap"):
            n_table[0] += 1
            add_table("Supporting information", wrap, n_table[0])
