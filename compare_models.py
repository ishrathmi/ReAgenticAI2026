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
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK_2, GRID = "#0b0b0b", "#52514e", "#e6e5e0"
KEEP = ["source_file", "location", "page", "doi", "paper_title", "source", "evidence_verified",
        "evidence_match", "sequence_source", "model"]


# --- loading -------------------------------------------------------------------------------

def load_runs(out_dir: Path, basename: str) -> dict:
    """{model: rows} from every output CSV; the newest file wins when a model appears twice."""
    found = {}
    for path in sorted(out_dir.glob(f"{basename}*.csv"), key=lambda p: p.stat().st_mtime):
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        model = rows[0].get("model") or path.stem.replace(basename, "").strip("_") or "unknown"
        found[model] = {"path": path, "rows": rows}
    return found


def rescore(rows: list, schema: dict, enrich) -> list:
    out = []
    for r in rows:
        rec = coerce(r, schema)
        rec.update({k: r.get(k) for k in KEEP})
        rec["evidence_verified"] = str(r.get("evidence_verified")).lower() == "true"
        try:
            rec["confidence"] = float(r["confidence"]) if r.get("confidence") else None
        except ValueError:
            rec["confidence"] = None
        out.append(enrich(rec))
    return out


def run_stats(out_dir: Path) -> dict:
    """Latest runs.csv row per model, plus mean table coverage from that run's event log."""
    table = out_dir / "runs" / "runs.csv"
    stats = {}
    if not table.exists():
        return stats
    with open(table, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status", "").startswith("ok") or row.get("status", "").startswith("stopped"):
                stats[row["model"]] = row
    for model, row in stats.items():
        log = out_dir / "runs" / f"{row['run_id']}.jsonl"
        covs = []
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if e.get("event") == "table_coverage":
                    covs.append(e["coverage"])
        row["mean_table_coverage"] = sum(covs) / len(covs) if covs else None
    return stats


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
    out["solvent"] = (not gsol and not rsol) or (bool(tokens) and all(t in rsol for t in tokens))
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
        "evidence found in source %": 100 * sum(bool(r.get("evidence_verified")) for r in recs) / n,
        "with sequence %": 100 * sum(bool(r.get("sequence")) for r in recs) / n,
        "numeric values with unit %": 100 * sum(bool(r.get("solubility_unit")) for r in numeric) / (len(numeric) or 1),
    }


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
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
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
        bars = ax.bar(x - 0.4 + width * (i + 0.5), ys_plot, width * 0.92, color=PALETTE[i % len(PALETTE)],
                      label=short(m), edgecolor="white", linewidth=1)
        for b, y in zip(bars, ys):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(), "n/a" if y is None else fmt.format(y),
                    ha="center", va="bottom", fontsize=7.5, color=INK_2)
    ax.set_xticks(x)
    ax.set_xticklabels([textwrap(g) for g in groups])
    if ymax is None:
        ax.set_ylim(0, top * 1.18 if top else 1)
    else:
        ax.set_ylim(0, ymax)


def textwrap(s: str, width: int = 16) -> str:
    import textwrap as tw
    return "\n".join(tw.wrap(s, width))


def short(model: str) -> str:
    return model.split("/")[-1]


def legend(fig_or_ax, models):
    from matplotlib.patches import Patch
    handles = [Patch(color=PALETTE[i % len(PALETTE)], label=short(m)) for i, m in enumerate(models)]
    fig_or_ax.legend(handles=handles, frameon=False, fontsize=9, loc="upper right", ncol=len(models))


def plot_all(out: Path, models, auto, agree, stats, gold_ok, scores, fields, n_gold, n_draft):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": "white", "axes.facecolor": "white"})

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
        cats = ["found by all models", "found by some other models", "found by this model only"]
        shades = ["#52514e", "#a3a29c", "#dedcd5"]          # neutral ramp: model colours keep their meaning
        import numpy as np
        y = np.arange(len(models))
        left = np.zeros(len(models))
        for c, col in zip(cats, shades):
            vals = np.array([agree[m][c] for m in models], dtype=float)
            ax.barh(y, vals, left=left, color=col, edgecolor="white", linewidth=2, label=c, height=0.6)
            for yi, (l, v) in enumerate(zip(left, vals)):
                if v:
                    ax.text(l + v / 2, yi, f"{int(v)}", ha="center", va="center", fontsize=8,
                            color="white" if col == shades[0] else INK)
            left += vals
        ax.set_yticks(y)
        ax.set_yticklabels([short(m) for m in models])
        ax.invert_yaxis()
        _style(ax, "Agreement between models (distinct records)")
        ax.yaxis.grid(False)
        ax.xaxis.grid(True, color=GRID)
        ax.set_xlabel("records", color=INK_2, fontsize=9)
        ax.legend(frameon=False, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, -0.42), ncol=3)

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
        fig.savefig(out / f"{name}.png", dpi=160)
        plt.close(fig)
    if gold_ok:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        fields_chart(ax)
        legend(ax, models)
        fig.tight_layout()
        fig.savefig(out / "field_accuracy.png", dpi=160)
        plt.close(fig)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8, 3.6))
    cost_time(a1, a2)
    legend(fig, models)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(out / "cost_time.png", dpi=160)
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
    fig.savefig(out / "summary.png", dpi=150, bbox_inches="tight")
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
    stats = run_stats(out_dir)
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

    out = out_dir / "comparison"
    out.mkdir(parents=True, exist_ok=True)
    plot_all(out, models, auto, agree, stats, bool(gold), scores, fields, len(gold), n_draft)
    write_workbook(out / "comparison.xlsx", models, auto, agree, stats, scores, fields, details)

    print(f"Compared {len(models)} models: {', '.join(models)}")
    if gold:
        print(f"Reference set: {len(gold)} records ({n_draft} draft)")
        for m in models:
            s = scores[m]
            p = "n/a" if s["precision %"] is None else f"{s['precision %']:.0f}%"
            fa = "n/a" if s["field accuracy %"] is None else f"{s['field accuracy %']:.0f}%"
            print(f"  {short(m):<28} recall {s['recall %']:.0f}%  precision {p}  field accuracy {fa}")
    print(f"Charts and workbook: {out}")
    return 0


def write_workbook(path, models, auto, agree, stats, scores, fields, details):
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
    for k in ["cost_usd", "wall_time_s", "inference_time_s", "llm_calls", "tokens_in", "tokens_out",
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
    wb.save(path)


if __name__ == "__main__":
    sys.exit(main())
