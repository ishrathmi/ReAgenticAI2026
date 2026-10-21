"""Finding a compound's drawing in a paper's figures (no image models needed)."""
import pymupdf
import pytest

from filips_agent import figures
from filips_agent.figures import Drawing


@pytest.fixture
def doc():
    d = pymupdf.open()
    page = d.new_page()  # 595 x 842
    page.draw_rect(pymupdf.Rect(60, 60, 300, 200), color=(0, 0, 0))  # the figure's graphics
    page.insert_text((80, 215), "IT-M")
    page.insert_text((200, 215), "ITIC")
    page.insert_text((60, 240), "Figure 1. a) Chemical structures of ITIC and IT-M. b) Absorption spectra.")
    page.insert_text((60, 300), "Figure 2 shows the J-V curves of the devices.")
    page.draw_rect(pymupdf.Rect(60, 400, 300, 500), color=(0, 0, 0))
    page.insert_text((60, 520), "Figure 3. J-V curves of IT-M devices.")
    page = d.new_page()
    page.draw_rect(pymupdf.Rect(60, 60, 500, 200), color=(0, 0, 0))
    page.insert_text((60, 220), "Scheme 1. Synthetic route to IT-M.")
    return d


def test_captions_skip_body_text(doc):
    assert [(f.kind, f.number, f.page) for f in figures.captions(doc)] == [
        ("figure", "1", 0), ("figure", "3", 0), ("scheme", "1", 1)]


def test_structure_captions_rank_above_plots_that_name_the_compound(doc):
    found = figures.find_figures(doc, ["IT-M"])
    assert [f.name for f in found] == ["Scheme 1 (p. 2)", "Figure 1 (p. 1)", "Figure 3 (p. 1)"]
    assert [round(v) for v in found[1].region] == [60, 60, 300, 200]


def test_location_from_the_extractor_comes_first(doc):
    assert figures.find_figures(doc, ["IT-M"], location="Figure 1a")[0].name == "Figure 1 (p. 1)"


def test_mentions_whole_names_only():
    assert figures.mentions("Chemical structures of ITIC, IT-M and IT-DM", "it-m")
    assert not figures.mentions("Chemical structures of ITIC, IT-DM", "IT-M")
    assert not figures.mentions("compound 12 and 3", "1")  # one-character labels are too risky


def test_text_labels_below_a_drawing(doc):
    assert figures.text_labels(doc[0], (70, 60, 150, 200)) == ["IT-M"]


def drawing(x, y, labels=()):
    return Drawing(0, (x, y, x + 50, y + 50), labels=list(labels))


def test_pick_by_label_then_final_product_then_all():
    a, b = drawing(0, 0, ["ITIC"]), drawing(100, 0, ["IT-M"])
    assert figures.pick([a, b], ["IT-M"], "figure") == ([b], "label")
    first, last = drawing(300, 0), drawing(0, 200)  # second row: later in the reaction sequence
    assert figures.pick([first, last], ["IT-M"], "scheme") == ([last], "final product of scheme")
    assert figures.pick([a, first], ["IT-M"], "figure") == ([a, first], "unlabelled, all candidates")
