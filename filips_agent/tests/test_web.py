import io
import time

import pytest

from filips_agent import pipeline, web
from filips_agent.ledger import AWAITING_PDF, Ledger
from filips_agent.normalize_values import normalize_record

THIOPHENE = "CCCCCCc1ccsc1"


@pytest.fixture
def app(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.db"
    with Ledger(ledger_path) as lg:
        lg.add("10.1000/x")
        rows, _ = normalize_record({"names": ["T-6"], "smiles": THIOPHENE, "source_doi": "10.1000/x",
                                    "values": [{"label": "IP", "value": 5.4, "origin": "measured", "technique": "UPS"}]})
        lg.save_compounds("10.1000/x", [(rows, ["structure uncertain (P3c): mixture"])])
        lg.add("10.1000/wish", AWAITING_PDF, title="A wished-for paper")
    monkeypatch.setattr(web.Ledger, "export", lambda self, out_dir=None: None)  # keep data/output untouched
    filed = []
    monkeypatch.setattr(pipeline, "do_inbox", lambda ledger, inbox=None: filed.append(sorted(p.name for p in inbox.glob("*"))) or ({}, {}))
    monkeypatch.setattr(pipeline, "apply_and_export", lambda ledger: None)
    a = web.create_app(ledger_path, tmp_path / "inbox")
    a.filed, a.ledger_path, a.inbox = filed, ledger_path, tmp_path / "inbox"
    return a


def wait_for_job(client):
    for _ in range(100):
        if not client.get("/api/state").json["job"]["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("job still running")


def test_state_lists_every_window(app):
    s = app.test_client().get("/api/state").json
    assert [r["name"] for r in s["review"]] == ["T-6"]
    assert [p["doi"] for p in s["wishlist"]] == ["10.1000/wish"]
    assert {p["doi"] for p in s["papers"]} == {"10.1000/x", "10.1000/wish"}
    assert s["data"] == [] and s["inbox"] == [] and s["job"]["running"] is False


def test_accept_with_an_edit_moves_the_record_to_data(app):
    client = app.test_client()
    rec = client.get("/api/state").json["review"][0]
    r = client.post("/api/review", json={"id": rec["id"], "decision": "accept", "edits": {"ip": "5.45"}})
    assert r.json["messages"] == [f"#{rec['id']} T-6: corrected (ip)"]
    s = client.get("/api/state").json
    assert s["review"] == [] and [(d["name"], d["ip"]) for d in s["data"]] == [("T-6", "5.45")]


def test_dropped_files_are_saved_and_filed(app):
    client = app.test_client()
    r = client.post("/api/upload", data={"files": [(io.BytesIO(b"%PDF-1.7"), "paper.pdf"),
                                                   (io.BytesIO(b"si"), "paper SI.docx")]})
    assert r.json["saved"] == ["paper.pdf", "paper_SI.docx"] and r.json["filing"]
    wait_for_job(client)
    assert app.filed == [["paper.pdf", "paper_SI.docx"]]


def test_files_dropped_on_a_wish_list_row_are_named_after_its_doi(app):
    client = app.test_client()
    r = client.post("/api/upload", data={"doi": "10.1000/wish", "files": [(io.BytesIO(b"%PDF"), "download.pdf"),
                                                                          (io.BytesIO(b"%PDF"), "esi.pdf")]})
    assert r.json["saved"] == ["10.1000_wish.pdf", "10.1000_wish_SI_esi.pdf"]


def test_other_file_types_are_refused(app):
    r = app.test_client().post("/api/upload", data={"files": [(io.BytesIO(b"x"), "data.xlsx")]})
    assert r.status_code == 400 and "only" in r.json["error"]


def test_one_job_at_a_time(app, monkeypatch):
    release = []
    monkeypatch.setattr(pipeline, "do_extract", lambda ledger, batch: [time.sleep(0.01) for _ in iter(lambda: release, [1])])
    client = app.test_client()
    assert client.post("/api/run", json={"action": "read", "batch": 2}).json == {"started": True}
    assert client.post("/api/run", json={"action": "triage"}).status_code == 409
    release.append(1)
    wait_for_job(client)
    job = client.get("/api/state").json["job"]
    assert job["name"] == "Reading up to 2 paper(s)" and job["error"] is None


def test_job_output_and_errors_reach_the_page(app, monkeypatch):
    def broken(ledger, batch):
        print("reading 10.1000/x")
        raise RuntimeError("PDF unreadable")
    monkeypatch.setattr(pipeline, "do_extract", broken)
    client = app.test_client()
    client.post("/api/run", json={"action": "read"})
    wait_for_job(client)
    job = client.get("/api/state").json["job"]
    assert "reading 10.1000/x" in job["log"] and job["error"] == "PDF unreadable"


def test_structure_drawing(app):
    client = app.test_client()
    r = client.get("/api/structure.svg", query_string={"smiles": THIOPHENE})
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<svg" in r.data
    assert client.get("/api/structure.svg", query_string={"smiles": "not a smiles"}).status_code == 404


def test_papers_list_their_si_files(app, tmp_path, monkeypatch):
    main, si = tmp_path / "10.1000_x.pdf", tmp_path / "10.1000_x_SI1.pdf"
    main.write_bytes(b"%PDF main")
    si.write_bytes(b"%PDF si")
    monkeypatch.setattr(web.fetch, "get_fulltext",
                        lambda doi: {"main": main, "si": [si]} if doi == "10.1000/x" else None)
    monkeypatch.setattr(web.fetch, "si_files", lambda doi: [si] if doi == "10.1000/x" else [])
    client = app.test_client()
    paper = next(p for p in client.get("/api/state").json["papers"] if p["doi"] == "10.1000/x")
    assert paper["has_pdf"] and paper["si"] == ["10.1000_x_SI1.pdf"]
    assert client.get("/pdf/10.1000/x", query_string={"si": 1}).data == b"%PDF si"
    assert client.get("/pdf/10.1000/x", query_string={"si": 2}).status_code == 404


def test_just_filed_says_paper_and_si(app, monkeypatch, tmp_path):
    from pathlib import Path
    filed = {"10.1000/x": [Path("10.1000_x.pdf"), Path("10.1000_x_SI1.pdf")], "10.1000/wish": [Path("10.1000_wish_SI1.pdf")]}
    monkeypatch.setattr(pipeline, "do_inbox", lambda ledger, inbox=None: (filed, {}))
    client = app.test_client()
    client.post("/api/upload", data={"files": [(io.BytesIO(b"%PDF"), "a.pdf")]})
    wait_for_job(client)
    just = {p["doi"]: (p["main"], p["si"]) for p in client.get("/api/state").json["just_filed"]}
    assert just == {"10.1000/x": (True, 1), "10.1000/wish": (False, 1)}


def test_reading_clears_the_just_filed_list(app, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(pipeline, "do_inbox", lambda ledger, inbox=None: ({"10.1000/x": [Path("10.1000_x.pdf")]}, {}))
    monkeypatch.setattr(pipeline, "do_extract", lambda ledger, batch: None)
    client = app.test_client()
    client.post("/api/upload", data={"files": [(io.BytesIO(b"%PDF"), "a.pdf")]})
    wait_for_job(client)
    assert len(client.get("/api/state").json["just_filed"]) == 1
    client.post("/api/run", json={"action": "read"})
    wait_for_job(client)
    assert client.get("/api/state").json["just_filed"] == []


def test_read_removes_what_couldnt_be_filed(app, monkeypatch):
    monkeypatch.setattr(pipeline, "do_inbox", lambda ledger, inbox=None: ({}, {"stuck.pdf": "no matching paper"}))
    monkeypatch.setattr(pipeline, "do_extract", lambda ledger, batch: None)
    app.inbox.mkdir()
    (app.inbox / "stuck.pdf").write_bytes(b"%PDF")
    client = app.test_client()
    client.post("/api/run", json={"action": "read"})
    wait_for_job(client)
    s = client.get("/api/state").json
    assert s["inbox"] == [] and "removed stuck.pdf: no matching paper" in s["job"]["log"]


def test_remove_all_empties_the_inbox(app):
    app.inbox.mkdir()
    for name in ("a.pdf", "b_SI.docx"):
        (app.inbox / name).write_bytes(b"x")
    client = app.test_client()
    assert client.post("/api/inbox/remove_all", json={}).json == {"removed": ["a.pdf", "b_SI.docx"]}
    assert client.get("/api/state").json["inbox"] == []
