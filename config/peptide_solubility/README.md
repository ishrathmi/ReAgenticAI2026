# Profile: peptide solubility

Extracts experimental peptide solubility data from main-text PDFs and SI files (.pdf, .xlsx, .docx) into `outputs/peptide_solubility_db.csv` and `.xlsx`.

## Run

```bash
python run_agent.py --profile peptide_solubility --dry-run   # which passages would be sent; no cost
python run_agent.py --profile peptide_solubility             # extract and write the database
```

## Pipeline

| Step | File | What it does |
|---|---|---|
| Gather | `agent.py` | Every .pdf/.xlsx/.docx under `local_folders`, plus ChemRxiv results if `queries` is set |
| Parse | `tools/parse_pdf.py` | Text per page; tables with their captions; tables inside Word text boxes |
| Filter | `tools/prefilter.py` | Keyword score per chunk (`prefilter.signals`); only high scorers go to the model |
| Extract | `tools/extract.py` | Prompt built from `schema.json`; JSON reply; truncated replies salvaged |
| Verify | `tools/extract.py` | Evidence quote must appear in the source text |
| Coverage | `agent.py` | Tables are sent 4 rows at a time; values missing from the reply are counted and flagged (`coverage` in papers.yaml) |
| Link | `agent.py` | Label-only records ("variant 7") get sequences from SI tables in the same `paper_groups` entry |
| Enrich | `domains/peptide_solubility.py` | One-letter sequence, MW, conversion to mg/mL, K → °C, review flags |
| Write | `output.py` | CSV + Excel (Records, Files processed, Field guide); flagged rows highlighted |

## Reviewing the output

Sort or filter the Excel sheet on `needs_review`. `review_reason` states why a row was flagged. `evidence` and `location` point to the exact passage.

## Cost control

- `llm.max_calls_per_run` stops the run at a fixed number of calls and saves partial results.
- Replies are cached in `.cache/llm/`; re-running on unchanged files costs nothing. Delete the folder after changing the prompt if you want fresh replies (prompt changes already miss the cache automatically).
- Raise `prefilter.min_score` to send fewer chunks.

## Known limitations

- Values shown only in figures (plots) are not extracted.
- Mole-fraction and molality values are kept as reported; converting them needs solvent density.
- MW conversion covers canonical residues with free, acetyl or amide termini.
- Scanned (image-only) PDFs need OCR first.

Failure types planned for the agent loop are listed in `docs/agent_loop_plan.md`.
