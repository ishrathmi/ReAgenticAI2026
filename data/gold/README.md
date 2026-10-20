# Reference set (gold standard) for peptide solubility extraction

`peptide_solubility_gold.csv` holds the correct records for selected sections of the papers in
`data/papers`. `compare_models.py` scores every model's output against it.

**Status: DRAFT.** The rows were drafted from the tables and text and must be checked by a person.
For each row, correct any field, then change `status` from `draft` to `confirmed`. Draft rows are
still scored; the report states how many rows are unconfirmed.

Open points to check:
- Word SI tables: the unit is not stated (likely mole fraction). Enter the unit once confirmed.
- Nat Commun Table 3: 'S' means no precipitation occurred; recorded as qualitative "soluble".
- Teixobactin paper: group statements ("prodrugs A and B") are recorded once per group.

Conventions:
- Leave a field empty when the paper does not state it. A model that fills it in is scored as wrong.
- One row per value: each table cell, each run, each condition.
- `where` is for people only (table and row); it is not used for matching.
- `scope.yaml` marks which files are annotated completely (needed for precision).
