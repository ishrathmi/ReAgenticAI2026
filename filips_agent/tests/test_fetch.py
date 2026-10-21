import json

import pymupdf
import pytest

from filips_agent import fetch
from filips_agent.ledger import AWAITING_PDF, TO_READ, Ledger


def raw_work(doi, wid, refs=(), abstract=None, pdf_url=None):
    return {
        "id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}", "title": f"Title {wid}",
        "publication_year": 2020, "primary_location": {"source": {"display_name": "Adv. Mater."}},
        "open_access": {"is_oa": bool(pdf_url)}, "best_oa_location": {"pdf_url": pdf_url} if pdf_url else None,
        "abstract_inverted_index": abstract, "referenced_works": [f"https://openalex.org/{r}" for r in refs],
    }


class FakeOpenAlex:
    """Answers /works filter queries from a dict of raw works and counts the calls."""

    def __init__(self, works):
        self.works, self.calls = works, []

    def __call__(self, path, **params):
        self.calls.append(params["filter"])
        kind, values = params["filter"].split(":", 1)
        values = values.split("|")
        if kind == "doi":
            hits = [w for w in self.works if (w["doi"] or "").removeprefix("https://doi.org/") in values]
        else:
            hits = [w for w in self.works if w["id"].rsplit("/", 1)[1] in values]
        return {"results": hits}


@pytest.fixture
def fake(monkeypatch):
    works = [
        raw_work("10.1/main", "W1", refs=["W2", "W3", "W4"], abstract={"Organic": [0], "semiconductors": [1], "rock": [2]}),
        raw_work("10.1/ref-a", "W2"),
        raw_work("10.1/ref-b", "W3"),
        {"id": "https://openalex.org/W4", "doi": None},  # a reference without DOI
    ]
    api = FakeOpenAlex(works)
    monkeypatch.setattr(fetch, "_openalex", api)
    return api


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.db") as lg:
        yield lg


def make_pdf(path, text):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)


# --- OpenAlex -----------------------------------------------------------------------

def test_abstract_from_index():
    assert fetch.abstract_from_index({"world": [1], "Hello": [0], "again": [3], "hello": [2]}) == "Hello world hello again"
    assert fetch.abstract_from_index(None) is None


def test_get_work_parses_and_caches(fake, tmp_path):
    work = fetch.get_work("https://doi.org/10.1/MAIN", tmp_path)
    assert work["title"] == "Title W1" and work["abstract"] == "Organic semiconductors rock"
    assert (work["journal"], work["year"], work["referenced_works"]) == ("Adv. Mater.", 2020, ["W2", "W3", "W4"])
    assert fetch.get_work("10.1/main", tmp_path) == work
    assert len(fake.calls) == 1  # second lookup came from the cache


def test_get_works_batches_and_skips_unknown(fake, tmp_path):
    works = fetch.get_works(["10.1/ref-a", "10.1/ref-b", "10.1/unknown"], tmp_path)
    assert set(works) == {"10.1/ref-a", "10.1/ref-b"}
    assert len(fake.calls) == 1


def test_reference_dois_keeps_order_drops_missing_and_caches(fake, tmp_path):
    work = fetch.get_work("10.1/main", tmp_path)
    assert fetch.reference_dois(work, tmp_path) == ["10.1/ref-a", "10.1/ref-b"]
    cached = json.loads((tmp_path / "10.1_main.json").read_text(encoding="utf-8"))
    assert cached["references"] == [{"doi": "10.1/ref-a", "title": "Title W2"}, {"doi": "10.1/ref-b", "title": "Title W3"}]


# --- full text and wish list (W6) ----------------------------------------------------

def test_acquire_wishlists_paper_without_oa_pdf(fake, ledger, tmp_path):
    ledger.add("10.1/main")
    assert fetch.acquire(ledger, "10.1/main", tmp_path / "pdfs", tmp_path / "cache") is None
    source = ledger.get("10.1/main")
    assert (source["status"], source["reason"], source["title"]) == (AWAITING_PDF, "no open-access PDF found", "Title W1")


def test_acquire_uses_existing_pdf_and_si(ledger, tmp_path):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    make_pdf(pdfs / "10.1_main.pdf", "main")
    make_pdf(pdfs / "10.1_main_SI1.pdf", "si")
    (pdfs / "10.1_main_SI2.mp4").write_bytes(b"video")  # P8a: not a document
    text = fetch.acquire(ledger, "10.1/main", pdfs, tmp_path / "cache")
    assert text["main"].name == "10.1_main.pdf"
    assert [p.name for p in text["si"]] == ["10.1_main_SI1.pdf"]


def test_download_rejects_html(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch, "_get", lambda url, tries=3: b"<html>Just a moment...</html>")
    work = {"doi": "10.1/x", "pdf_url": "https://example.org/x.pdf"}
    assert fetch.download_oa_pdf(work, tmp_path) is None
    monkeypatch.setattr(fetch, "_get", lambda url, tries=3: b"%PDF-1.7 ...")
    assert fetch.download_oa_pdf(work, tmp_path).name == "10.1_x.pdf"


# --- inbox ---------------------------------------------------------------------------

@pytest.fixture
def inbox(tmp_path):
    (tmp_path / "inbox").mkdir()
    return tmp_path / "inbox", tmp_path / "pdfs"


def test_inbox_files_pdfs_by_doi_on_page_and_by_name(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1002/adma.201602776", AWAITING_PDF)
    ledger.add("10.1038/s41560-019-0448-5", AWAITING_PDF)
    make_pdf(box / "adma201602776.pdf", "Adv. Mater. 2016  DOI: 10.1002/adma.201602776")
    make_pdf(box / "adma201602776-sup-0001-S1.pdf", "Supporting Information\nDOI: 10.1002/adma.201602776")
    make_pdf(box / "10.1038_s41560-019-0448-5.pdf", "no DOI printed here")
    make_pdf(box / "unrelated.pdf", "DOI: 10.9999/other")

    filed, skipped = fetch.collect_inbox(ledger, box, pdfs)

    assert sorted(p.name for p in filed["10.1002/adma.201602776"]) == [
        "10.1002_adma.201602776.pdf", "10.1002_adma.201602776_SI1.pdf"]
    assert [p.name for p in filed["10.1038/s41560-019-0448-5"]] == ["10.1038_s41560-019-0448-5.pdf"]
    assert [p.name for p in box.iterdir()] == ["unrelated.pdf"]
    assert "no matching paper" in skipped["unrelated.pdf"]
    assert ledger.get("10.1002/adma.201602776")["status"] == TO_READ
    assert ledger.get("10.1002/adma.201602776")["reason"] == "PDF provided"


def test_springer_nature_esm_is_si_even_with_doi_on_page_and_sorting_first(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1038/s41560-019-0448-5", AWAITING_PDF)
    ledger.add("10.1007/s40843-020-1639-x", AWAITING_PDF)
    make_pdf(box / "41560_2019_448_MOESM1_ESM.pdf", "Nature Energy  https://doi.org/10.1038/s41560-019-0448-5")
    make_pdf(box / "s41560-019-0448-5.pdf", "Article  https://doi.org/10.1038/s41560-019-0448-5")
    make_pdf(box / "40843_2020_1639_MOESM1_ESM.pdf", "Experimental details, no DOI")  # matched by name
    make_pdf(box / "s40843-020-1639-x.pdf", "Sci China Mater  https://doi.org/10.1007/s40843-020-1639-x")

    filed, skipped = fetch.collect_inbox(ledger, box, pdfs)

    assert skipped == {}
    assert sorted(p.name for p in filed["10.1038/s41560-019-0448-5"]) == [
        "10.1038_s41560-019-0448-5.pdf", "10.1038_s41560-019-0448-5_SI1.pdf"]
    assert sorted(p.name for p in filed["10.1007/s40843-020-1639-x"]) == [
        "10.1007_s40843-020-1639-x.pdf", "10.1007_s40843-020-1639-x_SI1.pdf"]
    assert pymupdf.open(pdfs / "10.1038_s41560-019-0448-5.pdf")[0].get_text().startswith("Article")


def test_two_main_candidates_both_stay(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1/main", AWAITING_PDF)
    make_pdf(box / "a.pdf", "DOI: 10.1/main")
    make_pdf(box / "b.pdf", "DOI: 10.1/main")
    filed, skipped = fetch.collect_inbox(ledger, box, pdfs)
    assert filed == {} and set(skipped) == {"a.pdf", "b.pdf"}
    assert ledger.get("10.1/main")["status"] == AWAITING_PDF


def test_inbox_si_alone_keeps_paper_waiting(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1/main", AWAITING_PDF)
    make_pdf(box / "10.1_main_SI.pdf", "Supporting Information")
    fetch.collect_inbox(ledger, box, pdfs)
    assert ledger.get("10.1/main")["status"] == AWAITING_PDF


def fake_sorter(**answers):
    """Sorter that answers by file name; unknown files get no opinion beyond agreeing."""
    calls = []

    def sort(filename, text, wanted):
        calls.append(filename)
        return answers[filename]
    sort.calls = calls
    return sort


def test_sorter_disagreement_leaves_file_in_inbox(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1000/main", AWAITING_PDF, title="A paper")
    make_pdf(box / "paper.pdf", "DOI: 10.1000/main")
    sorter = fake_sorter(**{"paper.pdf": {"doi": "10.1000/main", "kind": "si", "reason": "figures S1-S9"}})
    filed, skipped = fetch.collect_inbox(ledger, box, pdfs, sorter=sorter)
    assert filed == {}
    assert skipped["paper.pdf"].startswith("rules say main, sorter says si")
    assert sorter.calls == ["paper.pdf"]


def test_sorter_finds_paper_by_title_when_rules_cannot(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1/main", AWAITING_PDF, title="Wide-gap acceptor")
    make_pdf(box / "download (3).pdf", "Wide-gap acceptor ... no DOI")
    sorter = fake_sorter(**{"download (3).pdf": {"doi": "10.1/main", "kind": "main", "reason": "title matches"}})
    filed, _ = fetch.collect_inbox(ledger, box, pdfs, sorter=sorter)
    assert [p.name for p in filed["10.1/main"]] == ["10.1_main.pdf"]


def test_sorter_other_is_left(ledger, inbox):
    box, pdfs = inbox
    ledger.add("10.1/main", AWAITING_PDF)
    make_pdf(box / "letter.pdf", "DOI: 10.1/main  Dear editor")
    sorter = fake_sorter(**{"letter.pdf": {"doi": None, "kind": "other", "reason": "cover letter"}})
    filed, skipped = fetch.collect_inbox(ledger, box, pdfs, sorter=sorter)
    assert filed == {} and "cover letter" in skipped["letter.pdf"]


def test_si_detection_does_not_match_thesis():
    from pathlib import Path
    assert not fetch._looks_like_si(Path("phd_thesis.pdf"), "")
    assert fetch._looks_like_si(Path("paper_ESI.pdf"), "")
    assert fetch._looks_like_si(Path("adma-sup-0001.pdf"), "")
    assert fetch._looks_like_si(Path("41560_2019_448_MOESM1_ESM.pdf"), "")


# --- papers dropped in that the ledger doesn't know yet -------------------------------

TITLES = {"10.1000/new": "Fused Ring Acceptors for Organic Solar Cells", "10.1000/cited": "Something Else Entirely"}


@pytest.fixture
def openalex_titles(monkeypatch):
    monkeypatch.setattr(fetch, "get_work",
                        lambda doi, cache_dir=None: {"doi": doi, "title": TITLES[doi]} if doi in TITLES else None)


def test_new_paper_is_identified_by_doi_and_title_on_its_first_page(ledger, inbox, openalex_titles):
    box, pdfs = inbox
    # the cited DOI comes first on the page, but its title isn't printed there
    make_pdf(box / "download.pdf", "see 10.1000/cited\nFused Ring Acceptors for Organic Solar Cells\nDOI: 10.1000/new")
    make_pdf(box / "download_SI.pdf", "Supporting Information\n10.1000/new")
    make_pdf(box / "scan.pdf", "no DOI on this page")

    assert fetch.identify_dropped(ledger, box) == {"download.pdf": ("10.1000/new", True)}
    assert ledger.get("10.1000/new")["status"] == TO_READ
    assert ledger.get("10.1000/cited") is None
    filed, skipped = fetch.collect_inbox(ledger, box, pdfs)
    assert sorted(p.name for p in filed["10.1000/new"]) == ["10.1000_new.pdf", "10.1000_new_SI1.pdf"]
    assert list(skipped) == ["scan.pdf"]


def test_doi_typed_in_for_a_file_puts_it_on_the_reading_list(ledger, inbox, openalex_titles):
    box, pdfs = inbox
    make_pdf(box / "scan.pdf", "no DOI on this page")
    assert fetch.assign_doi(ledger, "scan.pdf", "https://doi.org/10.1000/new", box).name == "10.1000_new.pdf"
    assert ledger.get("10.1000/new")["title"] == TITLES["10.1000/new"]
    filed, _ = fetch.collect_inbox(ledger, box, pdfs)
    assert [p.name for p in filed["10.1000/new"]] == ["10.1000_new.pdf"]


def test_doi_typed_in_is_checked(ledger, inbox, openalex_titles):
    box, _ = inbox
    make_pdf(box / "scan.pdf", "x")
    with pytest.raises(ValueError, match="not a DOI"):
        fetch.assign_doi(ledger, "scan.pdf", "hello", box)
    ledger.add("10.1000/new", "done")
    with pytest.raises(ValueError, match="already in the ledger as done"):
        fetch.assign_doi(ledger, "scan.pdf", "10.1000/new", box)


def test_si_dropped_without_its_paper_says_so_and_can_be_given_the_papers_doi(ledger, inbox, openalex_titles):
    box, pdfs = inbox
    make_pdf(box / "esi.pdf", "Supporting Information\nno DOI printed")
    _, skipped = fetch.collect_inbox(ledger, box, pdfs)
    assert "looks like supporting information" in skipped["esi.pdf"]
    assert fetch.assign_doi(ledger, "esi.pdf", "10.1000/new", box).name == "10.1000_new_SI_esi.pdf"
    make_pdf(box / "paper.pdf", "Fused Ring Acceptors for Organic Solar Cells\nDOI: 10.1000/new")
    filed, _ = fetch.collect_inbox(ledger, box, pdfs)
    assert sorted(p.name for p in filed["10.1000/new"]) == ["10.1000_new.pdf", "10.1000_new_SI1.pdf"]


def test_paper_dropped_again_is_left_with_its_status_or_read_again(ledger, inbox, openalex_titles, monkeypatch):
    from filips_agent import pipeline
    box, pdfs = inbox
    monkeypatch.setattr(fetch, "get_fulltext", lambda doi, pdf_dir=pdfs: {"main": pdfs / "x.pdf", "si": []}
                        if doi == "10.1000/new" else None)
    monkeypatch.setattr(fetch, "collect_inbox", lambda ledger, inbox, sorter=None, leave=None:
                        ({}, dict(leave or {})))
    ledger.add("10.1000/new", "irrelevant", reason="no compound with structure and energy data (P7a)")
    make_pdf(box / "again.pdf", "Fused Ring Acceptors for Organic Solar Cells\nDOI: 10.1000/new")
    _, skipped = pipeline.do_inbox(ledger, use_llm=False, inbox=box)
    assert skipped["again.pdf"].startswith("already have this paper: 10.1000/new was read before (irrelevant: no compound")

    ledger.add("10.1000/cited", "irrelevant", reason="abstract")  # known, but its PDF was never filed
    TITLES["10.1000/cited"] = "Something Else Entirely"
    make_pdf(box / "cited.pdf", "Something Else Entirely\nDOI: 10.1000/cited")
    pipeline.do_inbox(ledger, use_llm=False, inbox=box)
    assert ledger.get("10.1000/cited")["status"] == TO_READ


def test_damaged_pdf_is_left_with_a_reason(ledger, inbox):
    box, pdfs = inbox
    (box / "broken.pdf").write_bytes(b"not a pdf at all")
    assert fetch.identify_dropped(ledger, box) == {}
    _, skipped = fetch.collect_inbox(ledger, box, pdfs)
    assert "can't open this file" in skipped["broken.pdf"]


def test_si_dropped_for_a_paper_waiting_for_it_puts_it_back_on_the_reading_list(ledger, inbox):
    box, pdfs = inbox
    pdfs.mkdir()
    make_pdf(pdfs / "10.1000_x.pdf", "main")
    ledger.add("10.1000/x", "awaiting_si", reason="SI needed: SI Table 1")
    make_pdf(box / "10.1000_x_SI_esi.pdf", "Supporting Information")
    filed, _ = fetch.collect_inbox(ledger, box, pdfs)
    assert [p.name for p in filed["10.1000/x"]] == ["10.1000_x_SI1.pdf"]
    assert (ledger.get("10.1000/x")["status"], ledger.get("10.1000/x")["reason"]) == (TO_READ, "SI provided")
