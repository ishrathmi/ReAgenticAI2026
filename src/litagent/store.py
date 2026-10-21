"""Paper store: every paper the agent can consult, kept in one SQLite file.

For each paper the store holds its metadata, its files (main text and SI, grouped), and every
chunk of every file (text passages and tables) with page, location label and section. A full-text
index (SQLite FTS5) lets tools search all files of a paper at once.

A file is parsed once: re-running on an unchanged file with the same parsing settings reuses the
stored chunks. The same store is used by the pipeline, the agent's tools and (later) the web app.

  store = PaperStore(".cache/papers.sqlite")
  paper_id, file_id = store.ingest_file("data/papers/x.pdf", paper_key="x", meta={...})
  store.search(paper_id, "variant 7")          # [(Chunk, snippet), ...]
  store.section(paper_id, "methods")           # [Chunk, ...]
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .tools.parse_pdf import PARSER_VERSION, SI_NAME_RE, Chunk, parse_document

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,      -- stable id derived from paper_key
    paper_key TEXT UNIQUE,          -- e.g. a paper_groups name, 'PMC1234567', or a file name
    title TEXT, doi TEXT, source TEXT, license TEXT, year TEXT, authors TEXT, meta TEXT, added TEXT);
CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY, paper_id TEXT, name TEXT, role TEXT, sha1 TEXT, settings TEXT,
    title TEXT, doi TEXT, added TEXT, UNIQUE (paper_id, name));
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id INTEGER PRIMARY KEY, file_id INTEGER, paper_id TEXT, ord INTEGER, page INTEGER,
    kind TEXT, label TEXT, section TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks (file_id, ord);
CREATE INDEX IF NOT EXISTS chunks_paper ON chunks (paper_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='chunk_id');
"""


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def paper_id_for(key: str) -> str:
    return "P" + hashlib.sha1(key.encode()).hexdigest()[:10]


class PaperStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)   # web app may use threads
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- adding papers ----------------------------------------------------------------------
    def upsert_paper(self, paper_key: str, meta: dict | None = None) -> str:
        meta = meta or {}
        pid = paper_id_for(paper_key)
        row = self.db.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
        fields = {k: meta.get(k) or (row[k] if row else "") for k in ("title", "doi", "source", "license", "year", "authors")}
        self.db.execute(
            "INSERT INTO papers (paper_id, paper_key, title, doi, source, license, year, authors, meta, added) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET title=excluded.title, "
            "doi=excluded.doi, source=excluded.source, license=excluded.license, year=excluded.year, "
            "authors=excluded.authors, meta=excluded.meta",
            (pid, paper_key, fields["title"], fields["doi"], fields["source"], fields["license"], fields["year"],
             fields["authors"], json.dumps(meta, default=str), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        return pid

    def ingest_file(self, path, paper_key: str, meta: dict | None = None, role: str | None = None,
                    chunk_chars: int = 2500, table_rows: int | None = None) -> tuple:
        """Parse a file into the store (or reuse the stored chunks). Returns (paper_id, file_id)."""
        path = Path(path)
        pid = self.upsert_paper(paper_key, meta)
        sha1 = _sha1(path)
        settings = f"v{PARSER_VERSION};chars={chunk_chars};rows={table_rows}"
        row = self.db.execute("SELECT * FROM files WHERE paper_id=? AND name=?", (pid, path.name)).fetchone()
        if row and row["sha1"] == sha1 and row["settings"] == settings:
            return pid, row["file_id"]
        doc = parse_document(path, chunk_chars, table_rows)
        role = role or ("si" if SI_NAME_RE.search(path.name) else "main")
        if row:
            self._delete_file(row["file_id"])
        cur = self.db.execute(
            "INSERT INTO files (paper_id, name, role, sha1, settings, title, doi, added) VALUES (?,?,?,?,?,?,?,?)",
            (pid, path.name, role, sha1, settings, doc.title, doc.doi, datetime.now().isoformat(timespec="seconds")))
        fid = cur.lastrowid
        for i, c in enumerate(doc.chunks):
            cur = self.db.execute(
                "INSERT INTO chunks (file_id, paper_id, ord, page, kind, label, section, text) VALUES (?,?,?,?,?,?,?,?)",
                (fid, pid, i, c.page, c.kind, c.label, c.section, c.text))
            self.db.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, c.text))
        # fill paper title/DOI from the main file if the metadata did not provide them
        p = self.paper(pid)
        if role == "main" and (not p["title"] or not p["doi"]):
            self.db.execute("UPDATE papers SET title=COALESCE(NULLIF(title,''), ?), doi=COALESCE(NULLIF(doi,''), ?) "
                            "WHERE paper_id=?", (doc.title, doc.doi, pid))
        self.db.commit()
        return pid, fid

    def _delete_file(self, file_id: int) -> None:
        for r in self.db.execute("SELECT chunk_id, text FROM chunks WHERE file_id=?", (file_id,)).fetchall():
            self.db.execute("INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', ?, ?)",
                            (r["chunk_id"], r["text"]))
        self.db.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        self.db.execute("DELETE FROM files WHERE file_id=?", (file_id,))

    # -- reading ------------------------------------------------------------------------------
    def _chunk(self, r) -> Chunk:
        c = Chunk(r["name"], r["page"], r["kind"], r["label"], r["text"], section=r["section"] or "",
                  chunk_id=r["chunk_id"], paper_id=r["paper_id"])
        return c

    _SELECT = ("SELECT c.chunk_id, c.paper_id, c.page, c.kind, c.label, c.section, c.text, c.ord, f.name "
               "FROM chunks c JOIN files f ON f.file_id = c.file_id ")

    def paper(self, paper_id: str):
        return self.db.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()

    def files(self, paper_id: str) -> list:
        return self.db.execute("SELECT * FROM files WHERE paper_id=? ORDER BY role, name", (paper_id,)).fetchall()

    def file(self, file_id: int):
        return self.db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()

    def file_chunks(self, file_id: int) -> list:
        rows = self.db.execute(self._SELECT + "WHERE c.file_id=? ORDER BY c.ord", (file_id,)).fetchall()
        return [self._chunk(r) for r in rows]

    def paper_chunks(self, paper_id: str) -> list:
        rows = self.db.execute(self._SELECT + "WHERE c.paper_id=? ORDER BY f.role, f.name, c.ord",
                               (paper_id,)).fetchall()
        return [self._chunk(r) for r in rows]

    def chunk(self, chunk_id: int) -> Chunk | None:
        r = self.db.execute(self._SELECT + "WHERE c.chunk_id=?", (chunk_id,)).fetchone()
        return self._chunk(r) if r else None

    def find_chunk(self, paper_id: str, file_name: str, label: str) -> Chunk | None:
        r = self.db.execute(self._SELECT + "WHERE c.paper_id=? AND f.name=? AND c.label=?",
                            (paper_id, file_name, label)).fetchone()
        return self._chunk(r) if r else None

    def neighbours(self, chunk_id: int, before: int = 1, after: int = 1) -> list:
        r = self.db.execute("SELECT file_id, ord FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        if not r:
            return []
        rows = self.db.execute(self._SELECT + "WHERE c.file_id=? AND c.ord BETWEEN ? AND ? ORDER BY c.ord",
                               (r["file_id"], r["ord"] - before, r["ord"] + after)).fetchall()
        return [self._chunk(x) for x in rows]

    def sections(self, paper_id: str) -> list:
        names = []
        for (s,) in self.db.execute("SELECT DISTINCT section FROM chunks WHERE paper_id=?", (paper_id,)):
            for part in (s or "").split("; "):
                if part and part not in names:
                    names.append(part)
        return names

    def section(self, paper_id: str, name: str) -> list:
        rows = self.db.execute(self._SELECT + "WHERE c.paper_id=? AND lower(c.section) LIKE ? "
                               "ORDER BY f.role, f.name, c.ord", (paper_id, f"%{name.lower()}%")).fetchall()
        return [self._chunk(r) for r in rows]

    def paper_text(self, paper_id: str) -> str:
        return "\n".join(c.text for c in self.paper_chunks(paper_id))

    def search(self, paper_id: str, query: str, limit: int = 5) -> list:
        """Full-text search within one paper (all its files). Returns [(Chunk, snippet)]."""
        words = [w for w in re.findall(r"[\w.]+", query) if len(w) > 1]
        if not words:
            return []
        fts = " ".join(f'"{w}"' for w in words)              # all words, any order
        try:
            rows = self.db.execute(
                self._SELECT.replace("SELECT ", "SELECT snippet(chunks_fts, 0, '[', ']', ' … ', 24) AS snip, ", 1)
                + "JOIN chunks_fts ON chunks_fts.rowid = c.chunk_id WHERE chunks_fts MATCH ? AND c.paper_id=? "
                "ORDER BY bm25(chunks_fts) LIMIT ?", (fts, paper_id, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:                                          # fall back to a plain substring search
            rows = self.db.execute(
                self._SELECT.replace("SELECT ", "SELECT '' AS snip, ", 1)
                + "WHERE c.paper_id=? AND lower(c.text) LIKE ? LIMIT ?",
                (paper_id, f"%{query.lower()}%", limit)).fetchall()
        out = []
        for r in rows:
            c = self._chunk(r)
            snip = r["snip"] or _snippet(c.text, query)
            out.append((c, snip))
        return out

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {"papers": q("SELECT count(*) FROM papers"), "files": q("SELECT count(*) FROM files"),
                "chunks": q("SELECT count(*) FROM chunks")}


def _snippet(text: str, query: str, width: int = 160) -> str:
    i = text.lower().find(query.lower())
    if i < 0:
        return text[: 2 * width]
    return text[max(0, i - width): i + len(query) + width]
