"""Compare model runs on the peptide solubility task and plot the results.

  python compare_models.py                      # all outputs/peptide_solubility_db*.csv
  python compare_models.py --profile peptide_solubility --gold data/gold/peptide_solubility_gold.csv

Writes to outputs/comparison/:
  summary.png            one-page overview (the four charts below)
  accuracy.png           recall, precision and field accuracy against the reference set
  automatic_checks.png   checks that need no reference set
  agreement.png          records found by all models, by some, or by one model only
  cost_time.png          cost and run time per model
  comparison.xlsx        every number behind the charts, plus the record-level matches

All model outputs are re-scored with the current code, so older runs are judged by the same rules.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from litagent import domains  # noqa: E402
from litagent.agent import label_key  # noqa: E402
from litagent.config import load_profile  # noqa: E402
from litagent.domains.peptide_solubility import to_one_letter  # noqa: E402
from litagent.tools.extract import coerce  # noqa: E402

# Categorical palette, fixed order (validated: CVD and normal-vision separation pass for 4 slots).
# Slots 3-4 are below 3:1 contrast on white, so every bar carries a visible value label.
# Colours: the project palette only (Coolors). Background is plain white.
#   Bright Marine #296eb4, Copperwood #b1740f, Deep Twilight #1b065e, Sunflower Gold #fdb833, Muted Teal #8db38b
# Series order checked with the dataviz validator: every pair separates clearly for normal and colour-blind
# vision (worst pair Muted Teal / Sunflower Gold, delta-E 11.9 protan, 17.4 normal). Deep Twilight and Sunflower
# Gold sit outside the usual lightness band, and Sunflower Gold (1.7:1) and Muted Teal (2.3:1) are faint on white,
# so every bar carries a value label and a thin Deep Twilight outline. A 6th+ model reuses the colours as
# hatched bars instead of introducing new colours.
MARINE, COPPER, TWILIGHT, GOLD, TEAL = "#296eb4", "#b1740f", "#1b065e", "#fdb833", "#8db38b"
PALETTE = [MARINE, COPPER, TWILIGHT, GOLD, TEAL]
INK = INK_2 = TWILIGHT
GRID = None                                    # no gridlines: value labels carry the numbers
KEEP = ["source_file", "location", "page", "doi", "paper_title", "source", "evidence_verified",
        "evidence_match", "sequence_source", "model", "paper_mentions_c_amide", "repair_outcome",
        "passage_has_sequence", "passage_has_cosolvent_percent"]


# --- loading -------------------------------------------------------------------------------

def load_runs(out_dir: Path, basename: str) -> dict:
    """{model: rows} from every output CSV; the newest file wins when a model appears twice."""
    found = {}
    for path in sorted(out_dir.glob(f"{basename}*.csv"), key=lambda p: p.stat().st_mtime):
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        # run_label separates "model (pipeline)" from "model + agent ..." in runs that used the review agent
        model = rows[0].get("run_label") or rows[0].get("model") or path.stem.replace(basename, "").strip("_") or "unknown"
        found[model] = {"path": path, "rows": rows}
    return found


def rescore(rows: list, schema: dict, enrich) -> list:
    out = []
    for r in rows:
        rec = coerce(r, schema)
        rec.update({k: r.get(k) for k in KEEP})
        rec["evidence_verified"] = str(r.get("evidence_verified")).lower() == "true"
        uis = str(r.get("unit_in_source", "")).lower()
        rec["unit_in_source"] = True if uis == "true" else False if uis == "false" else None
        for k in ("paper_mentions_c_amide", "passage_has_sequence", "passage_has_cosolvent_percent"):
            rec[k] = str(r.get(k)).lower() == "true"
        try:
            rec["confidence"] = float(r["confidence"]) if r.get("confidence") else None
        except ValueError:
            rec["confidence"] = None
        out.append(enrich(rec))
    return out


def run_stats(out_dir: Path) -> list:
    """All completed runs from runs.csv (oldest first), each with its mean table coverage from the event log."""
    table = out_dir / "runs" / "runs.csv"
    if not table.exists():
        return []
    with open(table, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("status", "").startswith(("ok", "stopped"))]
    for row in rows:
        log = out_dir / "runs" / f"{row['run_id']}.jsonl"
        covs = []
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if e.get("event") == "table_coverage":
                    covs.append(e["coverage"])
        row["mean_table_coverage"] = sum(covs) / len(covs) if covs else None
    return rows


def run_for_file(path: Path, model: str, rows: list, runs: list) -> dict:
    """The run behind an output file, for cost and time.

    1. The run_id written into the file (outputs from 21 Oct 2026 on); otherwise the run of the same
       extraction model that finished closest to the file's save time.
    2. If that run mostly reused saved replies (cache hits > model calls), its cost says little about the
       model, so the latest earlier run of the same model with mostly fresh calls is used instead.
       stats_note records which run was used.
    """
    from datetime import datetime
    same = [r for r in runs if r.get("model") == model]
    rid = next((r.get("run_id") for r in rows if r.get("run_id")), None)
    chosen = next((r for r in same if r["run_id"] == rid), None)
    if chosen is None:
        saved = datetime.fromtimestamp(path.stat().st_mtime)
        gaps = []
        for r in same:
            try:
                gaps.append((abs((saved - datetime.fromisoformat(r["finished"])).total_seconds()), r))
            except (KeyError, ValueError):
                pass
        chosen = min(gaps, key=lambda g: g[0])[1] if gaps else None
    if chosen is None:
        return {}
    note = f"run {chosen['run_id']}"
    if float(chosen.get("cache_hits") or 0) > float(chosen.get("llm_calls") or 0):
        fresh = [r for r in same if r["run_id"] <= chosen["run_id"]
                 and float(r.get("cache_hits") or 0) <= float(r.get("llm_calls") or 0)
                 and bool(r.get("agent_model")) == bool(chosen.get("agent_model"))]
        if fresh:
            note = f"run {fresh[-1]['run_id']} (run {chosen['run_id']} that wrote the file mostly reused saved replies)"
            chosen = fresh[-1]
    return {**chosen, "stats_note": note}


def label_stats(label: str, row: dict) -> dict:
    """Run statistics for one output label. For 'model (pipeline)' the agent's share is subtracted,
    so pipeline-only and pipeline + agent are compared on cost and time as well as accuracy."""
    row = dict(row)
    if not row or not label.endswith("(pipeline)"):
        return row
    for total, part in (("cost_usd", "agent_cost_usd"), ("wall_time_s", "agent_inference_s"),
                        ("inference_time_s", "agent_inference_s"), ("llm_calls", "agent_calls")):
        try:
            row[total] = float(row.get(total) or 0) - float(row.get(part) or 0)
        except ValueError:
            pass
    return row


# --- record identity ------------------------------------------------------------------------

def _name(s) -> str:
    s = re.sub(r"\([^)]*\)", "", str(s or "")).lower().replace("–", "-")
    s = re.sub(r"\b(the|corresponding|analogues?|o-acyl|isopeptide|series)\b", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def peptide_id(rec: dict) -> str:
    """Label number if present ('variant 7' -> 'L:7'), else one-letter sequence, else the cleaned name."""
    name = rec.get("peptide_name") or ""
    lab = label_key(name)
    if lab and re.fullmatch(r"\d+[a-z]?", lab):
        return f"L:{lab}"
    seq = to_one_letter(rec.get("sequence")) or to_one_letter(name)
    if seq:
        return f"S:{seq}"
    return f"N:{_name(name)}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def value_id(rec: dict) -> str:
    v = _num(rec.get("solubility_value"))
    if v is not None:
        return f"v:{v:.3g}"
    return f"q:{(rec.get('solubility_qualitative') or '').lower()}"


def same_peptide(a: str, b: str) -> bool:
    if a == b:
        return True
    if a.startswith("N:") and b.startswith("N:"):
        x, y = a[2:], b[2:]
        return bool(x and y) and (x in y or y in x or difflib.SequenceMatcher(None, x, y).ratio() >= 0.8)
    return False


def condition_score(g: dict, r: dict) -> float:
    """Tie-break between candidates with the same peptide and value (e.g. 0.077 in three tables)."""
    s = 0.0
    gs, rs = (g.get("solvent") or "").lower(), (r.get("solvent") or "").lower()
    for token in re.findall(r"dmso|ethanol|water|pbs|\d+%", gs):
        s += token in rs
    gt, rt = _num(g.get("temperature_c")), _num(r.get("temperature_c"))
    if gt is not None and rt is not None and abs(gt - rt) <= 0.5:
        s += 2
    if gt is None and rt is None:
        s += 0.5
    return s


def match(gold: list, recs: list) -> list:
    """One-to-one matching of reference rows to records from the same file. Returns [(g, r or None)].

    Pass 1 accepts only identical peptide IDs; pass 2 allows similar names for rows still unmatched,
    so 'teixobactin' cannot take the record that belongs to 'Arg10-teixobactin'.
    """
    pool = defaultdict(list)
    for r in recs:
        pool[r.get("source_file")].append(r)
    result = {}
    for fuzzy in (False, True):
        for i, g in enumerate(gold):
            if i in result:
                continue
            gp, gv = peptide_id(g), value_id(g)
            cands = [r for r in pool[g["source_file"]] if value_id(r) == gv
                     and (same_peptide(gp, peptide_id(r)) if fuzzy else gp == peptide_id(r))]
            if cands:
                best = max(cands, key=lambda r: condition_score(g, r))
                pool[g["source_file"]].remove(best)
                result[i] = best
    return [(g, result.get(i)) for i, g in enumerate(gold)]


# --- field checks ---------------------------------------------------------------------------

def _unit(u) -> str:
    u = str(u or "").lower().replace(" ", "").replace("−", "-").replace("ml-1", "/ml")
    return {"x": "molefraction", "x1": "molefraction", "mg/ml": "mg/ml", "g/l": "mg/ml"}.get(u, u)


def field_checks(g: dict, r: dict) -> dict:
    """True/False per field. An empty reference field means 'not stated': the record must leave it empty too."""
    out = {}
    out["unit"] = _unit(g.get("solubility_unit")) == _unit(r.get("solubility_unit"))
    gt, rt = _num(g.get("temperature_c")), _num(r.get("temperature_c"))
    out["temperature"] = (gt is None and rt is None) or (gt is not None and rt is not None and abs(gt - rt) <= 0.5)
    gsol = g.get("solvent") or ""
    tokens = re.findall(r"dmso|ethanol|water|pbs|\d+%", gsol.lower())
    rsol = (r.get("solvent") or "").lower()
    # every solvent named in the reference must be in the record, and the record must not add a co-solvent
    extra = set(re.findall(r"dmso|ethanol|methanol|acetonitrile|hfip|tfe|dmf", rsol)) - set(tokens)
    out["solvent"] = (not gsol and not rsol) or (bool(tokens) and all(t in rsol for t in tokens) and not extra)
    if g.get("sequence"):
        out["sequence"] = to_one_letter(g["sequence"]) == (r.get("sequence_one_letter") or to_one_letter(r.get("sequence")))
    gc = (g.get("c_terminus") or "").lower()
    rc = (r.get("c_terminus") or "").lower()
    out["c_terminus"] = gc == rc or (gc == "amide" and "amid" in rc)
    gq = (g.get("solubility_qualitative") or "").lower()
    rq = (r.get("solubility_qualitative") or "").lower()
    out["qualitative"] = gq == rq or (not gq)
    return out


# --- metrics --------------------------------------------------------------------------------

def automatic(recs: list) -> dict:
    n = len(recs) or 1
    numeric = [r for r in recs if r.get("solubility_value") is not None]
    keys = Counter((r.get("source_file"), peptide_id(r), value_id(r)) for r in recs)
    dupes = sum(c - 1 for c in keys.values())
    return {
        "records": len(recs),
        "duplicate records %": 100 * dupes / n,
        "flagged for review %": 100 * sum(bool(r.get("needs_review")) for r in recs) / n,
        "with data caveat %": 100 * sum(bool(r.get("data_caveats")) for r in recs) / n,
        "corrected by repair %": 100 * sum(r.get("repair_outcome") == "accepted" for r in recs) / n,
        "evidence found in source %": 100 * sum(bool(r.get("evidence_verified")) for r in recs) / n,
        "with sequence %": 100 * sum(bool(r.get("sequence")) for r in recs) / n,
        "numeric values with unit %": 100 * sum(bool(r.get("solubility_unit")) for r in numeric) / (len(numeric) or 1),
    }


def _rule(reason: str) -> str:
    """'unit 'mg/mL' not found in source text' -> 'unit '…' not found in source text'; numbers -> N."""
    s = re.sub(r"'[^']*'", "'…'", reason)
    s = re.sub(r"\d+(?:\.\d+)?", "N", s)
    return s.split(" (")[0].strip()


def flag_diagnosis(gold: list, recs: list) -> tuple:
    """How well needs_review separates wrong records from correct ones, overall and per rule.

    A record counts as wrong if any field check fails against its matched reference row.
    A useful rule flags mostly wrong records; a rule whose flagged records are rarely wrong adds work.
    """
    pairs = [(g, r) for g, r in match(gold, recs) if r is not None]
    wrong = [(g, r) for g, r in pairs if not all(field_checks(g, r).values())]
    right = [(g, r) for g, r in pairs if all(field_checks(g, r).values())]
    summary = {
        "errors caught": sum(bool(r.get("needs_review")) for _, r in wrong),
        "errors total": len(wrong),
        "correct records flagged": sum(bool(r.get("needs_review")) for _, r in right),
        "correct records total": len(right),
    }
    rules = {}
    for g, r in pairs:
        is_wrong = not all(field_checks(g, r).values())
        for reason in filter(None, str(r.get("review_reason") or "").split("; ") if r.get("needs_review") else []):
            k = _rule(reason)
            n, e = rules.get(k, (0, 0))
            rules[k] = (n + 1, e + is_wrong)
    missed = Counter(f for g, r in wrong if not r.get("needs_review")
                     for f, ok in field_checks(g, r).items() if not ok)
    return summary, rules, dict(missed)


def agreement(per_model: dict) -> dict:
    """For each model: unique records found by all models, by some others, or only by this model."""
    keysets = {m: {(r.get("source_file"), peptide_id(r), value_id(r)) for r in recs} for m, recs in per_model.items()}
    out = {}
    for m, ks in keysets.items():
        others = [o for o in keysets if o != m]
        c = Counter()
        for k in ks:
            n = sum(k in keysets[o] for o in others)
            c["found by all models" if n == len(others) and others else ("found by some other models" if n else "found by this model only")] += 1
        out[m] = c
    return out


def gold_scores(gold: list, recs: list, scope: dict) -> tuple:
    pairs = match(gold, recs)
    matched = [(g, r) for g, r in pairs if r is not None]
    complete = {f for f, s in scope.items() if s.get("complete")}
    in_scope = [r for r in recs if r.get("source_file") in complete]
    matched_ids = {id(r) for _, r in matched}
    # duplicates of a matched record (same file, peptide and value) are not counted as false positives
    matched_keys = {(r.get("source_file"), peptide_id(r), value_id(r)) for _, r in matched}
    false_pos = [r for r in in_scope if id(r) not in matched_ids
                 and (r.get("source_file"), peptide_id(r), value_id(r)) not in matched_keys]
    tp_scope = sum(1 for _, r in matched if r.get("source_file") in complete)
    fields = defaultdict(list)
    for g, r in matched:
        for k, ok in field_checks(g, r).items():
            fields[k].append(ok)
    scores = {
        "recall %": 100 * len(matched) / (len(gold) or 1),
        "precision %": 100 * tp_scope / ((tp_scope + len(false_pos)) or 1) if complete else None,
    }
    field_acc = {k: 100 * sum(v) / len(v) for k, v in fields.items() if v}
    scores["field accuracy %"] = sum(field_acc.values()) / len(field_acc) if field_acc else None
    return scores, field_acc, pairs, false_pos


# --- plotting -------------------------------------------------------------------------------

def _style(ax, title, ylabel=None, ymax=None):
    ax.set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold", pad=10)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(TWILIGHT)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(False)
    ax.set_yticks([])                          # values are labelled on the bars
    if ylabel:
        ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    if ymax is not None:
        ax.set_ylim(0, ymax)


def grouped_bars(ax, groups: list, models: list, values: dict, fmt="{:.0f}", ymax=None):
    """values[model][group] -> number or None. One bar per model inside each group."""
    import numpy as np
    x = np.arange(len(groups))
    width = 0.8 / max(len(models), 1)
    top = 0
    for i, m in enumerate(models):
        ys = [values[m].get(g) for g in groups]
        ys_plot = [y if y is not None else 0 for y in ys]
        top = max(top, max(ys_plot or [0]))
        bars = ax.bar(x - 0.4 + width * (i + 0.5), ys_plot, width * 0.88, label=short(m), **series_style(i))
        for b, y in zip(bars, ys):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(), "n/a" if y is None else fmt.format(y),
                    ha="center", va="bottom", fontsize=7.5, color=INK_2)
    ax.set_xticks(x)
    ax.set_xticklabels([textwrap(g) for g in groups])
    if ymax is None:
        ax.set_ylim(0, top * 1.18 if top else 1)
    else:
        ax.set_ylim(0, ymax)


def series_style(i: int) -> dict:
    """Bar style for model i: solid palette colour with a Deep Twilight outline; from the 5th model on,
    the same colours as hatching on the background (no new colours)."""
    col = PALETTE[i % len(PALETTE)]
    if i < len(PALETTE):
        return {"facecolor": col, "edgecolor": TWILIGHT, "linewidth": 0.6}
    return {"facecolor": "none", "edgecolor": col, "linewidth": 1.0, "hatch": "////"}


def text_on(fill: str) -> str:
    """Label colour for text drawn on a filled bar: the palette colour with the higher contrast."""
    return GOLD if fill == TWILIGHT else TWILIGHT     # Gold on Twilight 9.8:1; Twilight on Marine 3.2, Copper 4.4, Gold 9.8, Teal 7.3


def textwrap(s: str, width: int = 16) -> str:
    import textwrap as tw
    return "\n".join(tw.wrap(s, width))


def short(label: str) -> str:
    """'openai/gpt-4o-mini + agent openai/gpt-6.1-sol' -> 'gpt-4o-mini + agent gpt-6.1-sol'."""
    return re.sub(r"[\w.-]+/", "", label)


def extraction_model(label: str, rows: list) -> str:
    """Extraction model of an output: the part of the label before ' + agent' / ' (pipeline)'."""
    base = re.split(r" \+ agent | \(pipeline\)", label)[0].strip()
    return base or next((r.get("model") for r in rows if r.get("model")), label)


def legend(fig_or_ax, models):
    from matplotlib.patches import Patch
    handles = [Patch(label=short(m), **series_style(i)) for i, m in enumerate(models)]
    fig_or_ax.legend(handles=handles, frameon=False, fontsize=9, loc="upper right", ncol=len(models))


def snap_to_palette(path: Path) -> None:
    """Map every pixel to the nearest palette colour (or the white background), without dithering.

    matplotlib smooths text and edges with blended colours; snapping removes them, so the image contains
    only the project palette. Charts are saved at high resolution, so text stays sharp after snapping.
    """
    from PIL import Image
    colours = [MARINE, COPPER, TWILIGHT, GOLD, TEAL, "#ffffff"]
    pal = []
    for h in colours:
        pal += [int(h[k:k + 2], 16) for k in (1, 3, 5)]
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(pal + pal[-3:] * (256 - len(colours)))
    img = Image.open(path).convert("RGB").quantize(palette=pal_img, dither=Image.Dither.NONE)
    img.convert("RGB").save(path, optimize=True)


def plot_all(out: Path, models, auto, agree, stats, gold_ok, scores, fields, n_gold, n_draft):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": "white", "axes.facecolor": "white",
                         "text.color": TWILIGHT, "axes.labelcolor": TWILIGHT, "xtick.color": TWILIGHT,
                         "ytick.color": TWILIGHT, "axes.edgecolor": TWILIGHT, "hatch.linewidth": 1.0})

    def accuracy(ax):
        if not gold_ok:
            ax.text(0.5, 0.5, "No reference set found", ha="center", va="center", color=INK_2)
            ax.axis("off")
            return
        groups = ["recall %", "precision %", "field accuracy %"]
        grouped_bars(ax, groups, models, scores, ymax=122)
        sub = f"Against {n_gold} reference records" + (f" ({n_draft} still draft)" if n_draft else "")
        _style(ax, "Accuracy against the reference set\n" + sub, "%")
        ax.title.set_fontsize(10)

    def fields_chart(ax):
        if not gold_ok:
            ax.axis("off")
            return
        groups = sorted({k for m in models for k in fields[m]})
        grouped_bars(ax, groups, models, fields, ymax=122)
        _style(ax, "Field accuracy on matched records\n(empty = correctly left empty when not stated)", "%")
        ax.title.set_fontsize(10)

    def checks(ax):
        for m in models:
            cov = stats.get(m, {}).get("mean_table_coverage")
            auto[m]["mean table coverage %"] = None if cov is None else 100 * cov
        better = ["evidence found in source %", "with sequence %", "mean table coverage %"]
        worse = ["flagged for review %", "duplicate records %"]
        view = {m: {**{f"{g} (higher is better)": auto[m].get(g) for g in better},
                    **{f"{g} (lower is better)": auto[m].get(g) for g in worse}} for m in models}
        grouped_bars(ax, list(view[models[0]]), models, view, ymax=122)
        _style(ax, "Automatic checks (no reference set needed)", "% of records")

    def agree_chart(ax):
        """Each row keeps its model's colour; the fill shows agreement:
        solid = found by all models, hatched = found by some, outline only = this model only."""
        from matplotlib.patches import Patch
        import numpy as np
        cats = ["found by all models", "found by some other models", "found by this model only"]
        y = np.arange(len(models))
        gap = 0.6                                       # records-width gap between segments
        for yi, m in enumerate(models):
            col = PALETTE[yi % len(PALETTE)]
            left = 0.0
            for c in cats:
                v = float(agree[m][c])
                if not v:
                    continue
                style = ({"facecolor": col, "edgecolor": TWILIGHT, "linewidth": 0.6} if c == cats[0] else
                         {"facecolor": "none", "edgecolor": col, "linewidth": 1.2, "hatch": "////"} if c == cats[1] else
                         {"facecolor": "none", "edgecolor": col, "linewidth": 1.6})
                ax.barh(yi, v, left=left, height=0.6, **style)
                label_kw = {"color": text_on(col) if c == cats[0] else TWILIGHT}
                if c == cats[1]:                        # keep the number readable over the hatching
                    label_kw["bbox"] = {"boxstyle": "round,pad=0.15", "facecolor": "white", "edgecolor": "none"}
                ax.text(left + v / 2, yi, f"{int(v)}", ha="center", va="center", fontsize=8, fontweight="bold", **label_kw)
                left += v + gap
        ax.set_yticks(y)
        ax.set_yticklabels([short(m) for m in models])
        ax.invert_yaxis()
        _style(ax, "Agreement between models (distinct records)")
        ax.set_yticks(y)
        ax.set_yticklabels([textwrap(short(m), 22) for m in models], fontsize=8.5)
        ax.set_xlabel("records", color=INK_2, fontsize=9)
        handles = [Patch(facecolor=TWILIGHT, edgecolor=TWILIGHT, label=cats[0]),
                   Patch(facecolor="none", edgecolor=TWILIGHT, hatch="////", label=cats[1]),
                   Patch(facecolor="none", edgecolor=TWILIGHT, linewidth=1.6, label=cats[2])]
        ax.legend(handles=handles, frameon=False, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, -0.42), ncol=3)

    def cost_time(ax_cost, ax_time):
        cost = {m: {"cost": _num(stats.get(m, {}).get("cost_usd"))} for m in models}
        wall = {m: {"time": (_num(stats.get(m, {}).get("wall_time_s")) or 0) / 60 or None} for m in models}
        grouped_bars(ax_cost, ["cost"], models, cost, fmt="${:.2f}")
        _style(ax_cost, "Cost per run", "USD")
        ax_cost.set_xticks([])
        grouped_bars(ax_time, ["time"], models, wall, fmt="{:.1f} min")
        _style(ax_time, "Run time", "minutes")
        ax_time.set_xticks([])

    # individual charts
    for name, fn, size in [("accuracy", accuracy, (8, 4.5)), ("automatic_checks", checks, (11, 4.5)),
                           ("agreement", agree_chart, (8, 3.8))]:
        fig, ax = plt.subplots(figsize=size)
        fn(ax)
        if name != "agreement":
            legend(ax, models)
        fig.tight_layout()
        fig.savefig(out / f"{name}.png", dpi=240)
        snap_to_palette(out / f"{name}.png")
        plt.close(fig)
    if gold_ok:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        fields_chart(ax)
        legend(ax, models)
        fig.tight_layout()
        fig.savefig(out / "field_accuracy.png", dpi=240)
        snap_to_palette(out / "field_accuracy.png")
        plt.close(fig)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8, 3.6))
    cost_time(a1, a2)
    legend(fig, models)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(out / "cost_time.png", dpi=240)
    snap_to_palette(out / "cost_time.png")
    plt.close(fig)

    # one-page summary
    fig = plt.figure(figsize=(15, 10))
    gs = fig.add_gridspec(2, 4, height_ratios=[1, 1], hspace=0.55, wspace=0.6)
    accuracy(fig.add_subplot(gs[0, 0:2]))
    agree_chart(fig.add_subplot(gs[0, 2:4]))
    checks(fig.add_subplot(gs[1, 0:3]))
    ax_c = fig.add_subplot(gs[1, 3])
    cost = {m: {"cost": _num(stats.get(m, {}).get("cost_usd"))} for m in models}
    grouped_bars(ax_c, ["cost"], models, cost, fmt="${:.2f}")
    _style(ax_c, "Cost per run", "USD")
    ax_c.set_xticks([])
    legend(fig, models)
    fig.suptitle("Peptide solubility extraction: model comparison", x=0.01, ha="left", fontsize=14,
                 fontweight="bold", color=INK)
    fig.savefig(out / "summary.png", dpi=200, bbox_inches="tight")
    snap_to_palette(out / "summary.png")
    plt.close(fig)


# --- main -----------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="peptide_solubility")
    ap.add_argument("--gold", default="data/gold/peptide_solubility_gold.csv")
    ap.add_argument("--scope", default="data/gold/scope.yaml")
    ap.add_argument("--models", nargs="*", help="only these model IDs (default: all found)")
    args = ap.parse_args(argv)

    cfg, schema = load_profile(args.profile)
    out_dir = ROOT / cfg.get("output", {}).get("dir", "outputs")
    runs = load_runs(out_dir, cfg.get("output", {}).get("basename", args.profile))
    if args.models:
        runs = {m: v for m, v in runs.items() if m in args.models}
    if not runs:
        print("No model outputs found in", out_dir)
        return 1
    enrich = domains.load(cfg.get("domain"))
    models = sorted(runs)
    per_model = {m: rescore(runs[m]["rows"], schema, enrich) for m in models}
    all_runs = run_stats(out_dir)
    stats = {m: label_stats(m, run_for_file(runs[m]["path"], extraction_model(m, runs[m]["rows"]), runs[m]["rows"], all_runs))
             for m in models}
    auto = {m: automatic(per_model[m]) for m in models}
    agree = agreement(per_model)

    gold_path, gold = ROOT / args.gold, []
    if gold_path.exists():
        with open(gold_path, encoding="utf-8-sig") as f:
            gold = list(csv.DictReader(f))
    scope = (yaml.safe_load((ROOT / args.scope).read_text()) or {}).get("files", {}) if (ROOT / args.scope).exists() else {}
    scores, fields, details = {}, {}, {}
    for m in models:
        if gold:
            scores[m], fields[m], pairs, fps = gold_scores(gold, per_model[m], scope)
            details[m] = (pairs, fps)
    n_draft = sum(1 for g in gold if g.get("status", "draft") != "confirmed")
    diagnosis = {m: flag_diagnosis(gold, per_model[m]) for m in models} if gold else {}

    out = out_dir / "comparison"
    out.mkdir(parents=True, exist_ok=True)
    plot_all(out, models, auto, agree, stats, bool(gold), scores, fields, len(gold), n_draft)
    write_workbook(out / "comparison.xlsx", models, auto, agree, stats, scores, fields, details, diagnosis)

    print(f"Compared {len(models)} models: {', '.join(models)}")
    if gold:
        print(f"Reference set: {len(gold)} records ({n_draft} draft)")
        for m in models:
            s = scores[m]
            p = "n/a" if s["precision %"] is None else f"{s['precision %']:.0f}%"
            fa = "n/a" if s["field accuracy %"] is None else f"{s['field accuracy %']:.0f}%"
            print(f"  {short(m):<28} recall {s['recall %']:.0f}%  precision {p}  field accuracy {fa}")
        print("Review flag (needs_review) against the reference set:")
        for m in models:
            d, rules, missed = diagnosis[m]
            print(f"  {short(m):<28} catches {d['errors caught']}/{d['errors total']} wrong records; "
                  f"flags {d['correct records flagged']}/{d['correct records total']} correct records"
                  + (f"; unflagged errors in: {', '.join(f'{k} ({v})' for k, v in missed.items())}" if missed else ""))
            weak = [k for k, (n, e) in rules.items() if n >= 5 and e / n < 0.2]
            if weak:
                print(f"  {'':<28} rules that rarely find errors (<20% of flagged records wrong): {'; '.join(weak)}")
    print(f"Charts and workbook: {out}")
    return 0


def write_workbook(path, models, auto, agree, stats, scores, fields, details, diagnosis=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    rows = []
    metric_names = list(next(iter(auto.values())).keys())
    for k in metric_names:
        rows.append([k] + [auto[m].get(k) for m in models])
    for k in ["recall %", "precision %", "field accuracy %"]:
        if scores:
            rows.append([k] + [scores[m].get(k) for m in models])
    for k in sorted({f for m in fields for f in fields[m]}):
        rows.append([f"field: {k} %"] + [fields[m].get(k) for m in models])
    for c in ["found by all models", "found by some other models", "found by this model only"]:
        rows.append([c] + [agree[m][c] for m in models])
    for k in ["stats_note", "cost_usd", "wall_time_s", "inference_time_s", "llm_calls", "tokens_in", "tokens_out",
              "mean_latency_s", "mean_table_coverage"]:
        rows.append([k] + [stats.get(m, {}).get(k) for m in models])
    ws.append(["metric"] + models)
    for r in rows:
        ws.append([round(v, 3) if isinstance(v, float) else (_num(v) if isinstance(v, str) and _num(v) is not None else v) for v in r])
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 32
    for m, (pairs, fps) in details.items():
        s = wb.create_sheet(short(m)[:28])
        s.append(["gold_id", "status", "file", "where", "gold peptide", "gold value", "gold unit", "gold solvent",
                  "matched", "model peptide", "model value", "model unit", "model solvent", "model temp C",
                  "field errors"])
        for g, r in pairs:
            errs = "" if r is None else ", ".join(k for k, ok in field_checks(g, r).items() if not ok)
            s.append([g["gold_id"], g.get("status"), g["source_file"], g.get("where"), g["peptide_name"],
                      g.get("solubility_value") or g.get("solubility_qualitative"), g.get("solubility_unit"),
                      g.get("solvent"), "yes" if r else "MISSED",
                      None if r is None else r.get("peptide_name"),
                      None if r is None else (r.get("solubility_value") if r.get("solubility_value") is not None else r.get("solubility_qualitative")),
                      None if r is None else r.get("solubility_unit"), None if r is None else r.get("solvent"),
                      None if r is None else r.get("temperature_c"), errs])
        for r in fps:
            s.append(["EXTRA", "", r.get("source_file"), r.get("location"), "", "", "", "", "not in reference",
                      r.get("peptide_name"), r.get("solubility_value"), r.get("solubility_unit"), r.get("solvent"),
                      r.get("temperature_c"), ""])
        for c in s[1]:
            c.font = Font(bold=True)
    if diagnosis:
        s = wb.create_sheet("Flag diagnosis")
        s.append(["model", "rule (review_reason)", "matched records flagged", "of which wrong", "% wrong",
                  "verdict"])
        for m, (d, rules, missed) in diagnosis.items():
            s.append([short(m), "ALL FLAGS: wrong records caught", d["errors caught"], d["errors total"],
                      round(100 * d["errors caught"] / (d["errors total"] or 1), 1), ""])
            s.append([short(m), "ALL FLAGS: correct records flagged", d["correct records flagged"],
                      d["correct records total"],
                      round(100 * d["correct records flagged"] / (d["correct records total"] or 1), 1), ""])
            for k, (n, e) in sorted(rules.items(), key=lambda kv: -kv[1][0]):
                verdict = "useful" if e / n >= 0.4 else ("weak" if e / n >= 0.2 else "rarely finds errors")
                s.append([short(m), k, n, e, round(100 * e / n, 1), verdict if n >= 5 else "too few to judge"])
            for f, v in missed.items():
                s.append([short(m), f"UNFLAGGED errors in field: {f}", v, v, 100.0, "no rule catches these"])
        for c in s[1]:
            c.font = Font(bold=True)
        s.column_dimensions["B"].width = 60
    wb.save(path)


if __name__ == "__main__":
    sys.exit(main())
