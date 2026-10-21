# Creating a profile for a new subdiscipline

A profile is a folder `config/<topic>/` with `papers.yaml` and `schema.json`. The code stays the same.

## Steps

1. Copy the existing profile:
   ```bash
   cp -r config/peptide_solubility config/<topic>
   ```
2. In `config/<topic>/papers.yaml`:
   - `task`: one or two sentences saying what to extract, e.g. "You extract glass transition temperatures
     of polymers from papers and their supporting information."
   - `instructions`: field-specific rules (what to include and exclude, how to report ranges, units,
     uncertainties, computed versus measured values). Keep "never guess; use null when not stated".
   - `prefilter.signals` and `prefilter.required_pattern`: regex keywords and units for the property, so
     only relevant passages reach the model. Check with `--dry-run` that the right passages are selected.
   - `coverage.value_field`: the schema field holding the main numeric value; `exclude_columns`: table
     columns of calculated values.
   - `domain`: remove the line (or write a module, below). With `domain` removed, delete the
     domain-specific `agent.triggers` and `agent.hints` and keep the generic ones ("table only",
     "evidence quote not found", "low model confidence").
   - `output.basename`: e.g. `<topic>_db`. Set `local_folders` and `paper_groups` for the new papers.
3. In `config/<topic>/schema.json`: one property per field, each with `type` (use `["string", "null"]`
   so missing values are allowed) and a `description` the model can follow. Keep `evidence` and
   `confidence`. Fields filled by code, not the model, carry `"x-provenance": true`; keep the existing
   provenance fields (doi, paper_title, source, source_file, page, location).
4. Optional domain module `src/litagent/domains/<topic>.py` with `enrich(record) -> record` for unit
   conversion and checks that set `needs_review` / `review_reason`; optional `REPAIRS`, `paper_flags`,
   `passage_flags`, `paper_context` hooks (see `peptide_solubility.py`). Then set `domain: <topic>`.
5. Test:
   ```bash
   python run_agent.py --profile <topic> --dry-run
   python run_agent.py --profile <topic>            # start with one paper in local_folders
   ```
   Open the Excel output, read `review_reason` on flagged rows, and adjust instructions or checks.
6. For model comparison, create `data/gold/<topic>_gold.csv` (one row per correct value; columns as in
   the peptide reference set) and `scope.yaml`, then run
   `python compare_models.py --profile <topic> --gold data/gold/<topic>_gold.csv`.
   Note: `compare_models.py` matches peptides by label or sequence; other topics may need their own
   identity rule in `peptide_id()`.
