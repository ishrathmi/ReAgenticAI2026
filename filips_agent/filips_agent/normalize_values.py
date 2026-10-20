"""Turn raw extracted energy values into W2 table rows (RULES.md P2b, P5, P6, P7b, W2c).

The extractor reports values exactly as printed in the paper. Everything here is
deterministic: sign conventions, unit conversion, Fc re-anchoring and method labels.

Raw record (one compound, as produced by the extractor agent):

    {
      "names": ["PNDI2OD-T2", "P(NDI2OD-T2)"],
      "smiles": "...",
      "source_doi": "10.xxxx/...",
      "comment": "free text from the extractor",
      "values": [
        {
          "label": "HOMO" | "LUMO" | "IP" | "EA",   # as the paper calls it
          "value": -5.48,                         # as printed, any sign
          "unit": "eV" | "meV" | "kJ/mol",
          "origin": "dft" | "measured",
          "dft_method": "B3LYP/6-31G(d,p)",       # dft only
          "technique": "CV" | "UPS" | ... | None, # measured only; None = not stated
          "reference": "ferrocene" | "electrode" | None,   # CV only
          "fc_anchor": 4.8 | 5.1 | ... | None,    # eV the paper assigns to Fc/Fc+
          "plus_eg": False,                       # value derived via the optical gap
          "set": "CV film",                       # measured only: which measurement it
                                                  # belongs to; one output row per set
          "detail": "onset, film, 0.1 M Bu4NPF6/MeCN",
        },
      ],
    }
"""

NA = "n/a"
FC_ANCHOR = 4.8  # eV, Fc/Fc+ vs vacuum (P6b)
COLUMNS = ["name", "smiles", "homo_dft", "lumo_dft", "dft_method",
           "ip", "ip_method", "ea", "ea_method", "comment", "source_doi"]

_EV_PER = {"ev": 1.0, "mev": 1e-3, "kj/mol": 1 / 96.485}

# P2b: measured HOMO/LUMO are reported as IP/EA
_FIELD = {
    ("dft", "HOMO"): "homo_dft",
    ("dft", "LUMO"): "lumo_dft",
    ("measured", "HOMO"): "ip",
    ("measured", "LUMO"): "ea",
    ("measured", "IP"): "ip",
    ("measured", "EA"): "ea",
}
_IGNORED = {("dft", "IP"), ("dft", "EA")}  # P5c: computed IP/EA are not collected


def to_ev(value, unit="eV"):
    """Convert an energy to eV. Potentials (V) are not energies and are rejected."""
    try:
        return float(value) * _EV_PER[(unit or "eV").strip().lower()]
    except KeyError:
        raise ValueError(f"unsupported energy unit: {unit!r}") from None


def reanchor(value, anchor):
    """P6b: shift a CV-derived IP/EA (positive, eV) from the paper's Fc anchor to 4.8 eV."""
    return value + (FC_ANCHOR - anchor)


def method_label(technique, reference=None, fc_anchor=None, plus_eg=False):
    """P6a-P6d, P6g: build the ip_method / ea_method label.

    CV with ferrocene but no stated Fc energy can't be re-anchored, so it counts as
    "CV w/o ref" just like CV with no reference stated at all.
    """
    if not technique:
        label = "arbitrary"
    elif technique.strip().upper() == "CV":
        if reference == "ferrocene" and fc_anchor is not None:
            label = "CV"
        elif reference == "electrode":
            label = "CV w/o Fc"
        else:
            label = "CV w/o ref"
    else:
        label = technique.strip()
    return label + " + Eg" if plus_eg else label


def _fmt(x):
    return f"{x:.2f}"


def normalize_record(raw):
    """Return (rows, issues): one W2 row per measurement set, "n/a" for missing data.

    W2c: measured values are grouped by their "set" into separate rows; DFT values go
    on the first row only. Issues are reasons to send the compound to human review.
    """
    base = {c: NA for c in COLUMNS}
    names = [n.strip() for n in raw.get("names") or [] if n and n.strip()]
    if names:
        base["name"] = ", ".join(dict.fromkeys(names))  # P4a, keeps order, drops repeats
    for key in ("smiles", "source_doi"):
        if raw.get(key):
            base[key] = raw[key]

    issues = []
    dft, dft_methods, dft_notes = {}, [], []
    sets = {}  # set label -> {"values": {column: str}, "notes": [str]}, in order of appearance

    for v in raw.get("values") or []:
        label = (v.get("label") or "").strip().upper()
        origin = v.get("origin")
        if (origin, label) in _IGNORED:
            continue
        field = _FIELD.get((origin, label))
        if field is None:
            issues.append(f"cannot place {origin} {label or '?'} value in the table")
            continue
        try:
            energy = to_ev(v["value"], v.get("unit", "eV"))
        except (KeyError, TypeError, ValueError) as e:
            issues.append(f"{label}: unreadable value ({e})")
            continue

        if origin == "dft":
            tag = f"{label} (DFT)"
            if field in dft:
                issues.append(f"{tag}: more than one value ({dft[field]} and {_fmt(-abs(energy))})")
                continue
            dft[field] = _fmt(-abs(energy))  # P5a
            if v.get("dft_method"):
                dft_methods.append(v["dft_method"].strip())
            else:
                issues.append(f"{tag}: computational method not stated")
            if v.get("detail"):
                dft_notes.append(f"{tag}: {v['detail'].strip()}")
            continue

        tag = field.upper()
        energy = abs(energy)  # P6e
        method = method_label(v.get("technique"), v.get("reference"),
                              v.get("fc_anchor"), v.get("plus_eg", False))
        set_label = (v.get("set") or "").strip()
        group = sets.setdefault(set_label, {"values": {}, "notes": []})
        # within one set a column holds one value; a second means the sets weren't separated
        if field in group["values"]:
            where = f" in set {set_label!r}" if set_label else ""
            issues.append(f"{tag}: more than one value{where} "
                          f"({group['values'][field]} and {_fmt(energy)})")
            continue
        if method.split(" + ")[0] == "CV" and float(v["fc_anchor"]) != FC_ANCHOR:
            anchor = float(v["fc_anchor"])
            energy = reanchor(energy, anchor)
            group["notes"].append(f"{tag}: Fc anchored at {anchor:g} eV in paper, "
                                  f"adjusted by {FC_ANCHOR - anchor:+.2f} eV")
        group["values"][field] = _fmt(energy)
        group["values"][field + "_method"] = method
        if v.get("detail"):
            group["notes"].append(f"{tag}: {v['detail'].strip()}")

    extra = (raw.get("comment") or "").strip()
    rows = []
    for i, group in enumerate(list(sets.values()) or [{"values": {}, "notes": []}]):
        row = dict(base, **group["values"])
        notes = group["notes"]
        if i == 0:
            row.update(dft)
            if dft_methods:
                row["dft_method"] = "; ".join(dict.fromkeys(dft_methods))
            notes = dft_notes + notes + ([extra] if extra else [])
        if notes:
            row["comment"] = "; ".join(notes)
        rows.append(row)

    # P7: a compound needs a structure and at least one energy
    if base["smiles"] == NA:
        issues.append("no structure")
    if not dft and not sets:
        issues.append("no energy values")
    return rows, issues
