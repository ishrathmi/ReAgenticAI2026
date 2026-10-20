"""Convert papers and SI files into text chunks the model can read.

Supported: .pdf (text via PyMuPDF, tables via pdfplumber), .xlsx (every sheet as rows),
.docx (paragraphs and tables, no extra dependency). Figures are not interpreted.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

SUPPORTED = {".pdf", ".xlsx", ".docx"}
CAPTION_RE = re.compile(r"^\s*\[?(captions on this page: )?(Table|TABLE)\s+S?-?[0-9IVX]+", re.M)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+)", re.I)


@dataclass
class Chunk:
    source_file: str
    page: int | None     # 1-based page for PDFs; None for xlsx/docx
    kind: str            # "text" or "table"
    label: str           # human-readable location, e.g. "p4 text 2", "sheet Table S2"
    text: str
    score: int = 0


@dataclass
class Document:
    doc_id: str
    path: Path
    title: str = ""
    doi: str = ""
    chunks: list = field(default_factory=list)


def parse_pdf(path: str) -> str:
    """Plain text of a PDF (kept for the original scaffold interface)."""
    return "\n\n".join(c.text for c in parse_document(path).chunks if c.kind == "text")


def parse_document(path, chunk_chars: int = 2500) -> Document:
    path = Path(path)
    doc = Document(doc_id=hashlib.sha1(path.read_bytes()).hexdigest()[:10], path=path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        _read_pdf(doc, chunk_chars)
    elif suffix == ".xlsx":
        _read_xlsx(doc, chunk_chars)
    elif suffix == ".docx":
        _read_docx(doc, chunk_chars)
    else:
        raise ValueError(f"Unsupported file type: {path.name}")
    if not doc.doi:
        head = " ".join(c.text for c in doc.chunks[:3])
        m = DOI_RE.search(head)
        doc.doi = m.group(1).rstrip(".)") if m else ""
    if not doc.title:
        doc.title = path.stem
    _context_for_uncaptioned_tables(doc)
    return doc


def _context_for_uncaptioned_tables(doc: Document) -> None:
    """Give tables without a caption the file's other table captions as labelled context.

    Many SI files put several tables under one heading. The model is told the caption was
    not found, so it does not assume the listed caption belongs to this table.
    """
    caps = []
    for c in doc.chunks:
        if c.kind != "text":
            continue
        for m in CAPTION_RE.finditer(c.text):
            line = c.text[m.start():].split("\n", 1)[0][:200].strip(" []")
            if line not in caps:
                caps.append(line)
    if not caps:
        return
    note = "[No caption found for this table. Table captions elsewhere in this file: " + " / ".join(caps[:5]) + "]"
    for c in doc.chunks:
        if c.kind == "table" and not CAPTION_RE.search(c.text):
            c.text = f"{note}\n{c.text}"


# --- helpers -----------------------------------------------------------------

def _clean(text: str) -> str:
    text = text.replace("­", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _split(text: str, max_chars: int) -> list:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:                      # PDFs often lack blank lines; fall back to lines
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        while len(p) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if buf and len(buf) + len(p) + 1 > max_chars:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def _rows_to_text(rows) -> str:
    out = []
    for row in rows:
        cells = ["" if c is None else re.sub(r"\s+", " ", str(c)).strip() for c in row]
        if any(cells):
            out.append(" | ".join(cells))
    return "\n".join(out)


def _split_table(text: str, max_chars: int) -> list:
    """Split long tables by rows, repeating the header row in every piece."""
    lines = text.split("\n")
    if len(text) <= max_chars or len(lines) < 3:
        return [text]
    header, pieces, buf = lines[0], [], []
    for line in lines[1:]:
        if buf and len(header) + sum(len(x) + 1 for x in buf) + len(line) > max_chars:
            pieces.append("\n".join([header] + buf))
            buf = []
        buf.append(line)
    if buf:
        pieces.append("\n".join([header] + buf))
    return pieces


def _read_pdf(doc: Document, chunk_chars: int) -> None:
    import fitz  # PyMuPDF

    pdf = fitz.open(doc.path)
    doc.title = ((pdf.metadata or {}).get("title") or "").strip()
    captions = {}
    for i, page in enumerate(pdf, start=1):
        text = _clean(page.get_text("text"))
        captions[i] = " / ".join(m.group(0) for m in re.finditer(r"^\s*(Table|TABLE)\s+S?-?[0-9IVX]+.{0,200}", text, re.M))
        for j, piece in enumerate(_split(text, chunk_chars), start=1):
            doc.chunks.append(Chunk(doc.path.name, i, "text", f"p{i} text {j}", piece))
    pdf.close()

    try:
        import pdfplumber
    except ImportError:
        print("  [warn] pdfplumber not installed; PDF tables will be read as plain text only")
        return
    try:
        with pdfplumber.open(doc.path) as pl:
            for i, page in enumerate(pl.pages, start=1):
                for j, table in enumerate(page.extract_tables() or [], start=1):
                    for k, piece in enumerate(_split_table(_rows_to_text(table), chunk_chars), start=1):
                        if piece:
                            label = f"p{i} table {j}" + (f" part {k}" if k > 1 else "")
                            body = f"[captions on this page: {captions[i]}]\n{piece}" if captions.get(i) else piece
                            doc.chunks.append(Chunk(doc.path.name, i, "table", label, body))
    except Exception as exc:
        print(f"  [warn] table extraction failed for {doc.path.name}: {exc}")


def _read_xlsx(doc: Document, chunk_chars: int) -> None:
    from openpyxl import load_workbook

    wb = load_workbook(doc.path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        text = _rows_to_text(ws.iter_rows(values_only=True))
        for k, piece in enumerate(_split_table(text, chunk_chars), start=1):
            if piece:
                label = f"sheet '{ws.title}'" + (f" part {k}" if k > 1 else "")
                doc.chunks.append(Chunk(doc.path.name, None, "table", label, piece))
    wb.close()


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _read_docx(doc: Document, chunk_chars: int) -> None:
    """Walk the document in reading order, including tables inside text boxes.

    Word stores text boxes twice (mc:Choice and mc:Fallback); the Fallback copy is skipped.
    Each table is prefixed with the nearest preceding 'Table N ...' caption.
    """
    with zipfile.ZipFile(doc.path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras, tables, used_captions = [], [], set()

    def text_of(el):
        out = []
        for node in el.iter():
            if node.tag == f"{_W}t":
                out.append(node.text or "")
            elif node.tag == f"{_W}tab":
                out.append(" ")
        return "".join(out).strip()

    def walk(el):
        for child in el:
            if child.tag == _MC_FALLBACK:
                continue
            if child.tag == f"{_W}tbl":
                rows = [[text_of(tc) for tc in tr.findall(f"{_W}tc")] for tr in child.findall(f"{_W}tr")]
                caption = next((p for p in reversed(paras[-4:]) if CAPTION_RE.match(p)), "")
                if caption in used_captions:      # a caption belongs to one table only
                    caption = ""
                used_captions.add(caption)
                tables.append((caption, _rows_to_text(rows)))
            elif child.tag == f"{_W}p" and not any(n.tag == f"{_W}tbl" for n in child.iter()):
                t = text_of(child)
                if t:
                    paras.append(t)
            else:
                walk(child)

    walk(root)
    for n, (caption, text) in enumerate(tables, start=1):
        for k, piece in enumerate(_split_table(text, chunk_chars), start=1):
            if piece:
                label = f"table {n}" + (f" part {k}" if k > 1 else "")
                body = f"{caption}\n{piece}" if caption else piece
                doc.chunks.append(Chunk(doc.path.name, None, "table", label, body))
    for j, piece in enumerate(_split("\n\n".join(paras), chunk_chars), start=1):
        doc.chunks.append(Chunk(doc.path.name, None, "text", f"text {j}", piece))
