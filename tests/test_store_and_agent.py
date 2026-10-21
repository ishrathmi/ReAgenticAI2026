"""Offline tests: paper store, PDF table de-duplication, JATS reading, and the review agent loop.

No API key needed: the agent's model is a scripted fake that returns tool calls in order.
"""

import json

import fitz
from langchain_core.messages import AIMessage
from openpyxl import Workbook

from litagent import agent as pipeline
from litagent.config import load_profile
from litagent.review_agent import ReviewAgent, Task
from litagent.runlog import RunLogger
from litagent.store import PaperStore

RESULTS = """Results
Variant 7 was soluble at 2.5 mg/mL in water at 25 °C, as measured by UV absorbance of the supernatant."""
METHODS = """Methods
Peptide synthesis
All peptides were synthesised on Rink amide resin and obtained as C-terminal carboxamides."""


def _paper(tmp_path):
    folder = tmp_path / "papers"
    folder.mkdir()
    doc = fitz.open()
    for text in (RESULTS, METHODS):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10, fontname="helv")
    doc.save(folder / "paper_x9.pdf")
    wb = Workbook()
    wb.active.append(["Compound", "Sequence", "Mass"])
    wb.active.append([7, "KLVFFAE", 906.0])
    wb.save(folder / "paper_x9_si_001.xlsx")
    return folder


def test_store_sections_search_and_cache(tmp_path):
    folder = _paper(tmp_path)
    store = PaperStore(str(tmp_path / "s.sqlite"))
    pid, fid = store.ingest_file(folder / "paper_x9.pdf", "x9", {"source": "local file"})
    pid2, fid2 = store.ingest_file(folder / "paper_x9_si_001.xlsx", "x9")
    assert pid == pid2 and store.file(fid2)["role"] == "si"
    assert "Methods" in store.sections(pid) and "Results" in store.sections(pid)
    assert any("Rink amide" in c.text for c in store.section(pid, "methods"))
    hits = store.search(pid, "variant 7")
    assert hits and "Variant 7" in hits[0][0].text
    n = store.stats()["chunks"]
    assert store.ingest_file(folder / "paper_x9.pdf", "x9") == (pid, fid)        # unchanged file: reused
    assert store.stats()["chunks"] == n


def test_pdf_table_read_once(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Table 1 Solubility of the peptides", fontsize=10)
    cells = [["Peptide", "Run 1", "Run 2"], ["P1", "0.58", "1.38"], ["P2", "0.09", "0.16"], ["P3", "0.84", "0.82"]]
    x0, y0, w, h = 50, 80, 120, 22
    for r, row in enumerate(cells):
        for c, val in enumerate(row):
            rect = fitz.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y1 - 7), val, fontsize=9)
    page.insert_text((50, 200), "The peptides were soluble in water.", fontsize=10)
    path = tmp_path / "t.pdf"
    doc.save(path)
    from litagent.tools.parse_pdf import parse_document
    d = parse_document(path)
    tables = [c for c in d.chunks if c.kind == "table"]
    texts = " ".join(c.text for c in d.chunks if c.kind == "text")
    assert tables and "0.84" in tables[0].text
    assert "0.84" not in texts and "soluble in water" in texts and "Table 1" in texts


JATS = """<?xml version="1.0"?><article><front><article-meta>
<article-id pub-id-type="doi">10.1/abc</article-id><title-group><article-title>Peptide paper</article-title></title-group>
<abstract><p>We measured peptide solubility.</p></abstract></article-meta></front>
<body><sec><title>Results</title><p>P1 dissolved at 3 mg/mL in PBS.</p>
<table-wrap><label>Table 1</label><caption><p>Solubility in water</p></caption><table>
<tr><th>Peptide</th><th>mg/mL</th></tr><tr><td>P1</td><td>3.0</td></tr><tr><td>P2</td><td>0.5</td></tr></table></table-wrap></sec>
<sec sec-type="methods"><title>Methods</title><sec><title>Synthesis</title><p>Peptides were C-terminally amidated.</p></sec></sec>
</body></article>"""


def test_jats_reader(tmp_path):
    path = tmp_path / "PMC1.xml"
    path.write_text(JATS)
    store = PaperStore(str(tmp_path / "s.sqlite"))
    pid, fid = store.ingest_file(path, "PMC1", {"source": "Europe PMC"})
    chunks = store.file_chunks(fid)
    assert store.paper(pid)["title"] == "Peptide paper" and store.paper(pid)["doi"] == "10.1/abc"
    table = next(c for c in chunks if c.kind == "table")
    assert table.label == "Table 1" and "Solubility in water" in table.text and "P2 | 0.5" in table.text
    assert table.section == "Results"
    assert any("amidated" in c.text for c in store.section(pid, "methods"))
    assert {c.section for c in chunks} >= {"Abstract", "Results", "Methods"}


class ScriptedChat:
    """Fake chat model: returns the scripted AIMessages in order and records what it was sent."""
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0)


def _call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}", "type": "tool_call"}])


class ExtractModel:
    model = "fake-extractor"

    def chat(self, messages, **kw):
        if "Variant 7" in messages[-1]["content"]:
            return json.dumps({"records": [{"peptide_name": "Variant 7", "solubility_value": 2.5,
                                            "solubility_operator": "=", "solubility_unit": "mg/mL", "solvent": "water",
                                            "temperature_c": 25, "evidence": "Variant 7 was soluble at 2.5 mg/mL in water",
                                            "confidence": 0.9}]})
        return '{"records": []}'


def test_review_agent_fixes_terminus(tmp_path):
    folder = _paper(tmp_path)
    cfg, schema = load_profile("peptide_solubility")
    cfg = {**cfg, "local_folders": [str(folder)], "queries": [], "paper_groups": {"x9": ["x9"]},
           "repair": {"enabled": False}}
    cfg["agent"] = {**cfg["agent"], "cache_dir": str(tmp_path / "agentcache"), "max_steps_per_task": 5}
    store = PaperStore(str(tmp_path / "s.sqlite"))
    records, files = pipeline.run(cfg, schema, ExtractModel(), root=tmp_path, store=store)
    assert len(records) == 1
    rec = records[0]
    assert rec["sequence"] == "KLVFFAE"                     # linked from the SI table
    assert "C-terminal amidation" in rec["review_reason"]

    log = RunLogger(str(tmp_path / "runs"), "test", "fake")
    quote = "synthesised on Rink amide resin and obtained as C-terminal carboxamides"
    chat = ScriptedChat([
        _call("read_section", {"section": "methods", "query": "amide"}, 1),
        _call("update_record", {"record_ids": [0], "field": "c_terminus", "value": "amide",
                                "quote": "made as amides on Wang resin"}, 2),            # not in the paper: refused
        _call("update_record", {"record_ids": [0], "field": "c_terminus", "value": "amide", "quote": quote}, 3),
        _call("accept", {"reason": "Methods state all peptides were made as C-terminal amides."}, 4),
    ])
    reviewer = ReviewAgent(cfg, schema, store, records, extract_model=ExtractModel(), log=log, chat=chat, root=tmp_path)
    tasks = reviewer.build_tasks()
    assert [t.trigger for t in tasks] == ["C-terminal amidation"]
    result = reviewer.run()
    out = result["records"][0]
    assert out["c_terminus"] == "amide" and "C-terminal amidation" not in (out["review_reason"] or "")
    assert out["agent_status"] == "changed" and "chunk" in out["agent_changes"]
    assert result["tasks"][0].outcome == "accepted" and result["summary"]["agent_invalid_tool_calls"] == 1
    assert "Rink amide" in chat.seen[1][-1].content                # the tool result reached the model
    events = [json.loads(line)["event"] for line in log.path.read_text().splitlines()]
    assert events.count("agent_step") == 4 and "agent_task_end" in events


def test_find_sequence_tool(tmp_path):
    folder = _paper(tmp_path)
    cfg, schema = load_profile("peptide_solubility")
    cfg = {**cfg, "agent": {**cfg["agent"], "cache_dir": str(tmp_path / "c")}}
    store = PaperStore(str(tmp_path / "s.sqlite"))
    pid, _ = store.ingest_file(folder / "paper_x9.pdf", "x9")
    store.ingest_file(folder / "paper_x9_si_001.xlsx", "x9")
    reviewer = ReviewAgent(cfg, schema, store, [], chat=ScriptedChat([]), root=tmp_path)
    reviewer.task = Task("T1", pid, "paper_x9.pdf", None, "sequence not stated", "sequence not", [])
    out = reviewer.tool_map["find_sequence"].invoke({"label": "variant 7"})
    assert "KLVFFAE" in out and "quote" in out
