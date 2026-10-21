import pytest

from energy_extractor.normalize_values import (NA, basis_set_notation, method_label, normalize_record,
                                           reanchor, to_ev)


def cv(label, value, anchor=4.8, **kw):
    return {"label": label, "value": value, "origin": "measured", "technique": "CV",
            "reference": "ferrocene", "fc_anchor": anchor, **kw}


def dft(label, value, method="B3LYP/6-31G(d,p)"):
    return {"label": label, "value": value, "origin": "dft", "dft_method": method}


def record(*values, **kw):
    return {"names": ["X1"], "smiles": "c1ccsc1", "source_doi": "10.1/x", "values": list(values), **kw}


def one_row(raw):
    rows, issues = normalize_record(raw)
    assert len(rows) == 1
    return rows[0], issues


# --- units --------------------------------------------------------------------

def test_to_ev_units():
    assert to_ev(5.2) == 5.2
    assert to_ev(-5200, "meV") == pytest.approx(-5.2)
    assert to_ev(96.485, "kJ/mol") == pytest.approx(1.0)


def test_to_ev_rejects_potentials():
    with pytest.raises(ValueError):
        to_ev(0.42, "V")


# --- method labels (P6a-P6d, P6g) ---------------------------------------------

@pytest.mark.parametrize("args, label", [
    (("CV", "ferrocene", 4.8), "CV"),
    (("CV", "ferrocene", 5.1), "CV"),
    (("CV", "ferrocene", None), "CV w/o ref"),   # Fc used, but its energy not stated
    (("CV", "electrode", None), "CV w/o Fc"),
    (("CV", None, None), "CV w/o ref"),
    (("UPS",), "UPS"),
    ((None,), "arbitrary"),
    (("",), "arbitrary"),
])
def test_method_label(args, label):
    assert method_label(*args) == label


def test_method_label_plus_eg():
    assert method_label("CV", "ferrocene", 4.8, plus_eg=True) == "CV + Eg"
    assert method_label("UPS", plus_eg=True) == "UPS + Eg"
    assert method_label(None, plus_eg=True) == "arbitrary + Eg"


# --- Fc re-anchoring (P6b) ----------------------------------------------------

def test_reanchor():
    assert reanchor(5.42, 5.1) == pytest.approx(5.12)
    assert reanchor(5.00, 4.4) == pytest.approx(5.40)
    assert reanchor(5.30, 4.8) == pytest.approx(5.30)


def test_record_reanchors_cv_and_notes_it():
    row, issues = one_row(record(cv("HOMO", -5.42, anchor=5.1), cv("LUMO", -3.60, anchor=5.1)))
    assert (row["ip"], row["ea"]) == ("5.12", "3.30")
    assert row["ip_method"] == row["ea_method"] == "CV"
    assert "Fc anchored at 5.1 eV in paper, adjusted by -0.30 eV" in row["comment"]
    assert issues == []


def test_cv_without_fc_is_not_reanchored():
    row, _ = one_row(record(cv("IP", 5.24, anchor=4.4, reference="electrode")))
    assert (row["ip"], row["ip_method"]) == ("5.24", "CV w/o Fc")


def test_cv_plus_eg_is_reanchored_too():
    row, _ = one_row(record(cv("LUMO", -3.5, anchor=5.1, plus_eg=True)))
    assert (row["ea"], row["ea_method"]) == ("3.20", "CV + Eg")


# --- signs and P2b relabelling ------------------------------------------------

def test_measured_homo_lumo_become_positive_ip_ea():
    row, _ = one_row(record(cv("HOMO", -5.72), cv("LUMO", -3.83)))
    assert (row["ip"], row["ea"]) == ("5.72", "3.83")


def test_dft_values_are_negative_even_if_printed_positive():
    row, _ = one_row(record(dft("HOMO", 5.48), dft("LUMO", -3.15)))
    assert (row["homo_dft"], row["lumo_dft"]) == ("-5.48", "-3.15")
    assert row["dft_method"] == "B3LYP/6-31G(d,p)"


def test_value_without_method_is_arbitrary():
    row, issues = one_row(record({"label": "IP", "value": 5.3, "origin": "measured"}))
    assert (row["ip"], row["ip_method"]) == ("5.30", "arbitrary")
    assert issues == []


def test_computed_ip_ea_are_ignored():  # P5c
    row, issues = one_row(record(dft("IP", 6.1), dft("EA", 3.0), cv("HOMO", -5.5)))
    assert row["ip"] == "5.50"
    assert (row["homo_dft"], row["dft_method"]) == (NA, NA)
    assert issues == []


# --- several measurements: one row per set (W2c) ------------------------------

def test_film_and_solution_give_two_rows_with_dft_on_first_only():
    raw = record(dft("HOMO", -5.70),
                 cv("HOMO", -5.65, set="film", detail="onset, film"),
                 cv("LUMO", -3.90, set="film", plus_eg=True),
                 cv("HOMO", -5.72, set="solution", detail="onset, CH2Cl2 solution"))
    rows, issues = normalize_record(raw)
    assert issues == []
    assert [(r["ip"], r["ea"], r["ea_method"]) for r in rows] == [
        ("5.65", "3.90", "CV + Eg"), ("5.72", NA, NA)]
    assert [r["homo_dft"] for r in rows] == ["-5.70", NA]
    assert [r["comment"] for r in rows] == ["IP: onset, film", "IP: onset, CH2Cl2 solution"]
    assert all(r["name"] == "X1" and r["source_doi"] == "10.1/x" for r in rows)


def test_cv_and_ups_give_two_rows():
    ups = {"label": "IP", "value": 5.60, "origin": "measured", "technique": "UPS", "set": "UPS"}
    rows, issues = normalize_record(record(cv("HOMO", -5.40, set="CV"), ups))
    assert [(r["ip"], r["ip_method"]) for r in rows] == [("5.40", "CV"), ("5.60", "UPS")]
    assert issues == []


def test_sets_that_fill_different_columns_share_a_row():  # e.g. bisPCBM, 10.1002/adma.200702438
    no_method = {"label": "HOMO", "value": 6.1, "origin": "measured", "technique": None}
    row, issues = one_row(record(cv("LUMO", 3.7, set="CV"), no_method))
    assert (row["ip"], row["ip_method"], row["ea"], row["ea_method"]) == ("6.10", "arbitrary", "3.70", "CV")
    assert issues == []


def test_ups_ip_and_cv_ea_share_a_row():
    ups = {"label": "IP", "value": 5.60, "origin": "measured", "technique": "UPS", "set": "UPS"}
    row, _ = one_row(record(cv("LUMO", -3.70, set="CV"), ups))
    assert (row["ip_method"], row["ea_method"]) == ("UPS", "CV")


def test_two_values_in_one_set_go_to_review_and_first_is_kept():
    rows, issues = normalize_record(record(cv("HOMO", -5.40), cv("HOMO", -5.55)))
    assert [r["ip"] for r in rows] == ["5.40"]
    assert any("more than one value" in i for i in issues)


@pytest.mark.parametrize("printed, reported", [
    ("B3LYP/6-31G*", "B3LYP/6-31G(d)"),
    ("B3LYP/6-31G**", "B3LYP/6-31G(d,p)"),
    ("B3LYP/6-311+G*", "B3LYP/6-311+G(d)"),
    ("B3LYP/6-311++G**", "B3LYP/6-311++G(d,p)"),
    ("B3LYP/6-31G(d,p)", "B3LYP/6-31G(d,p)"),
    ("PBE0/def2-SVP", "PBE0/def2-SVP"),
])
def test_basis_set_notation(printed, reported):  # P5
    assert basis_set_notation(printed) == reported


def test_dft_method_uses_d_p_notation():
    row, _ = one_row(record(dft("HOMO", -5.2, method="B3LYP/6-31G*")))
    assert row["dft_method"] == "B3LYP/6-31G(d)"


def test_two_dft_values_for_one_column_go_to_review():
    _, issues = normalize_record(record(dft("HOMO", -5.2), dft("HOMO", -5.4, method="PBE0/def2-SVP")))
    assert any("HOMO (DFT): more than one value" in i for i in issues)


# --- full rows, n/a and issues (P4a, P7) ---------------------------------------

def test_gold_row_s41560_019_0448_5():
    raw = record(dft("HOMO", -5.48), dft("LUMO", -3.15), cv("HOMO", -5.72), cv("LUMO", -3.83),
                 source_doi="10.1038/s41560-019-0448-5")
    row, issues = one_row(raw)
    assert {k: row[k] for k in ("homo_dft", "lumo_dft", "dft_method", "ip", "ip_method", "ea", "ea_method")} == {
        "homo_dft": "-5.48", "lumo_dft": "-3.15", "dft_method": "B3LYP/6-31G(d,p)",
        "ip": "5.72", "ip_method": "CV", "ea": "3.83", "ea_method": "CV"}
    assert issues == []


def test_missing_values_are_na():
    row, _ = one_row(record({"label": "IP", "value": 5.4, "origin": "measured", "technique": "UPS"}))
    for col in ("homo_dft", "lumo_dft", "dft_method", "ea", "ea_method", "comment"):
        assert row[col] == NA


def test_synonyms_joined_without_repeats():
    row, _ = one_row(record(cv("IP", 5.5), names=["PNDI2OD-T2", "P(NDI2OD-T2)", "PNDI2OD-T2"]))
    assert row["name"] == "PNDI2OD-T2, P(NDI2OD-T2)"


def test_record_without_structure_or_energy_is_flagged():
    rows, issues = normalize_record({"names": ["1a"], "values": []})
    assert len(rows) == 1
    assert "no structure" in issues and "no energy values" in issues


def test_dft_without_method_is_flagged():
    _, issues = normalize_record(record(dft("HOMO", -5.2, method="")))
    assert any("computational method not stated" in i for i in issues)


def test_details_and_extractor_comment_go_to_comment():
    raw = record(cv("HOMO", -5.3, detail="onset, film, 0.1 M Bu4NPF6/MeCN"), comment="Annealed films")
    row, _ = one_row(raw)
    assert row["comment"] == "IP: onset, film, 0.1 M Bu4NPF6/MeCN; Annealed films"
