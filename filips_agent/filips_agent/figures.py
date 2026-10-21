"""Find the drawing of a compound in a paper's figures (RULES.md P3, PLAN.md structures route 3).

Two ways in, as a chemist would look:
- captions: "Figure 1. Chemical structures of ITIC, IT-M and IT-DM", or any caption naming the compound;
- schemes: in a synthesis scheme the compound is normally the final product.

A figure holds more than one thing (other molecules, device schematics, energy diagrams), so
each chemical drawing found in it is matched to the label printed next to it ("IT-M", "3a").
Without a matching label, a scheme gives its final product; anything else stays a list of
candidates. Plain pymupdf; the image models run elsewhere (ocsr.py).
"""
import re
from dataclasses import dataclass, field

import pymupdf

from .checks import name_key

_CAPTION = re.compile(r"^\s*(Figure|Fig\.?|Scheme|Chart)\s*(S?\d+)\s*[.:|]?\s*", re.I)
_STRUCTURE_WORDS = re.compile(r"(chemical|molecular)\s+structures?|(?<!device )structures?\s+of|synthe(sis|tic)",
                              re.I)
LABEL_GAP = 30  # pt below a drawing in which its label is looked for


@dataclass
class Figure:
    page: int                  # 0-based
    kind: str                  # figure, scheme or chart
    number: str                # "1", "S3"
    caption: str
    caption_rect: tuple
    score: int = 0
    region: tuple = None       # area holding the figure's graphics, PDF points

    @property
    def name(self):
        return f"{self.kind.capitalize()} {self.number} (p. {self.page + 1})"


@dataclass
class Drawing:
    """One chemical drawing in a figure, as found by segmentation, with what was read from it."""
    page: int
    rect: tuple                # PDF points
    image: str = None          # path of the cropped image
    labels: list = field(default_factory=list)   # text next to it (text layer and OCR)
    smiles: dict = field(default_factory=dict)   # tool name -> SMILES


def captions(doc):
    """All figure/scheme/chart captions in a document (body text like "Figure 3 shows" is skipped)."""
    found = []
    for i, page in enumerate(doc):
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            text = " ".join(text.split())
            m = _CAPTION.match(text)
            if not m or re.match(r"[a-z]{2,}", text[m.end():]):
                continue
            kind = m.group(1).lower().rstrip(".")
            kind = "figure" if kind.startswith("fig") else kind
            found.append(Figure(i, kind, m.group(2), text, (x0, y0, x1, y1)))
    return found


def mentions(text, name):
    """Does the text mention this compound name or label as a whole word?"""
    key = name_key(name)
    if len(key) < 2:
        return False
    words = [name_key(w) for w in re.split(r"[\s,;:()\[\]]+", text) if w]
    return key in words or (len(key) >= 4 and key in name_key(text))


def find_figures(doc, names, location=None):
    """Figures that may show the compound, best first: caption names it, or shows structures/synthesis.

    location: where the extractor saw the structure drawn ("Figure 1a", "Scheme 2"), tried first.
    """
    said = re.search(r"\b(fig(?:ure)?|scheme|chart)\.?\s*(S?\d+)", location or "", re.I)
    found = []
    for fig in captions(doc):
        if said and said.group(2).upper() == fig.number.upper() and said.group(1)[:3].lower() == fig.kind[:3]:
            fig.score = 10
            fig.region = figure_region(doc[fig.page], fig.caption_rect)
            found.append(fig)
            continue
        # "molecular structures" outweighs a mention: plots of the compound's data name it too
        fig.score = 3 if _STRUCTURE_WORDS.search(fig.caption) else 0
        fig.score += 1 if any(mentions(fig.caption, n) for n in names) else 0
        fig.score += 1 if fig.kind == "scheme" else 0
        if fig.score:
            fig.region = figure_region(doc[fig.page], fig.caption_rect)
            found.append(fig)
    return sorted(found, key=lambda f: -f.score)


def figure_region(page, caption_rect):
    """The graphics above the caption (or below it, for captions on top), in the caption's column."""
    cx0, cy0, cx1, cy1 = caption_rect
    graphics = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    graphics += [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
    graphics = [g for g in graphics if g.width > 5 and g.height > 5 and g.x1 > cx0 - 20 and g.x0 < cx1 + 20]
    for side in (lambda g: g.y1 <= cy0 + 5, lambda g: g.y0 >= cy1 - 5):
        picked = [g for g in graphics if side(g)]
        if picked:
            region = pymupdf.Rect(picked[0])
            for g in picked[1:]:
                region |= g
            return tuple(region)
    return tuple(page.rect)


def in_region(rect, region, margin=10):
    x = (rect[0] + rect[2]) / 2
    y = (rect[1] + rect[3]) / 2
    return region[0] - margin <= x <= region[2] + margin and region[1] - margin <= y <= region[3] + margin


def text_labels(page, rect):
    """Words of the text layer inside the drawing or just below it (vector figures)."""
    x0, y0, x1, y1 = rect
    words = [w for w in page.get_text("words")
             if w[0] < x1 and w[2] > x0 and y0 <= (w[1] + w[3]) / 2 <= y1 + LABEL_GAP]
    return [" ".join(w[4] for w in words)] if words else []


def pick(drawings, names, kind):
    """Choose this compound's drawing(s) among a figure's drawings. Returns (drawings, how)."""
    labelled = [d for d in drawings if any(mentions(t, n) for t in d.labels for n in names)]
    if labelled:
        return labelled, "label"
    if kind == "scheme" and drawings:
        # reading order: rows top to bottom, then left to right; the last one is the final product
        last = max(drawings, key=lambda d: (round((d.rect[1] + d.rect[3]) / 2 / 60), d.rect[0]))
        return [last], "final product of scheme"
    return list(drawings), "unlabelled; all candidates"
