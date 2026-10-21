---
name: litagent
description: Set up and run the ReAgenticAI2026 literature agent (pipeline, LangGraph review agent, Streamlit app, model comparison) that extracts chemistry data such as peptide solubility from papers and SI into a reviewed database. Use when asked to install, run, test, compare models, troubleshoot, or adapt it to a new subdiscipline.
---

# litagent: literature extraction agent

Extracts structured data (default profile: experimental peptide solubility) from papers and their
supporting information (PDF, XLSX, DOCX, Europe PMC full text) into a CSV/Excel database in which every
record carries a verified source quote. Repository: `ishrathmi/ReAgenticAI2026`.

## How the system works (read before acting)

1. **Pipeline** (`src/litagent/agent.py`): gather sources -> parse every file into the paper store
   (`store.py`, SQLite) -> keyword prefilter -> LLM extraction (`tools/extract.py`) -> checks (evidence
   quote in source, schema, table coverage, domain rules in `domains/<domain>.py`) -> one repair retry
   (`tools/repair.py`) for flagged records.
2. **Review agent** (`review_agent.py`, LangGraph): turns remaining flags into tasks and lets a model call
   tools (search_paper, read_section, read_nearby, find_sequence, re_extract, update_record, reject,
   accept). Code refuses any change whose quote is not found in the paper.
3. **Runner** (`runner.py`): one function used by the CLI and the web app; writes outputs and the run log.
4. **Configuration** lives in a profile, `config/<profile>/papers.yaml` + `schema.json`. Change behaviour
   there, not in code, unless the task needs new code.

## Rules

- Never commit or print `.env` (holds `OPENROUTER_API_KEY`). Never commit `data/papers/`, `outputs/`,
  `.cache/` or downloaded PDFs; `.gitignore` covers them.
- Use official APIs only (Europe PMC, ChemRxiv). Do not scrape publisher websites; subscription papers
  must be supplied by the user.
- Each run costs money. Run `--dry-run` first, tell the user how many passages will be sent, and keep the
  call limits (`llm.max_calls_per_run`, `agent.max_calls`) unless the user raises them.
- Do not move the paper store into a cloud-synced folder (OneDrive, Dropbox, iCloud): SQLite files there
  fail or get corrupted. Default: `~/.cache/litagent/papers.sqlite`.
- Run the offline tests after any code change: `python -m pytest` (no API key needed).

## 1. Set up (once per computer)

Requires Python 3.10+. Use a dedicated virtual environment, placed outside synced folders:

```bash
python3 -m venv ~/.venvs/reagenticai
source ~/.venvs/reagenticai/bin/activate          # Windows: %USERPROFILE%\.venvs\reagenticai\Scripts\activate
cd <path to ReAgenticAI2026>
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env                                # then the user adds their own OPENROUTER_API_KEY
python -m pytest                                    # expect all tests to pass
```

Ask the user to paste their key into `.env` themselves; do not ask them to send it to you.

## 2. Add papers

- Local files: put main text and SI files in `data/papers/` (any subfolders). Group each paper's files in
  `paper_groups` in `papers.yaml` (quoted file-name fragments; matching ignores case):
  ```yaml
  paper_groups:
    my_paper_2024:
      - "main-text-file-fragment"
      - "si-file-fragment"
  ```
  Unquoted fragments made of digits and underscores are read as numbers by YAML and fail.
- Europe PMC (open access, downloaded with SI): add to `queries` in `papers.yaml`:
  `- {source: europepmc, query: "peptide solubility", limit: 5}`.
- Remove duplicate copies of a paper; they double the cost and the records.

## 3. Run

```bash
python run_agent.py --profile peptide_solubility --dry-run          # passages per file; no cost
python run_agent.py --profile peptide_solubility                    # pipeline only
python run_agent.py --profile peptide_solubility --agent            # pipeline + review agent
python run_agent.py --profile peptide_solubility --model anthropic/claude-sonnet-5.5 --agent-model openai/gpt-6.1-sol --agent
```

Typical time on ~13 files: extraction 5-8 min fresh (1-2 min when cached replies are reused), repair
1-3 min, agent 5-18 min. Replies are cached in `.cache/llm` and `.cache/agent`, so re-runs on unchanged
files are nearly free. Watch progress with `tail -f outputs/runs/<run_id>.jsonl`.

Outputs in `outputs/`:
- `<basename>__<model>__pipeline.csv/.xlsx` (before the agent) and `...__agent-<agent-model>.csv/.xlsx`
  (after); without `--agent`, a single `<basename>__<model>.csv/.xlsx`.
- Excel sheets: Records (flagged rows highlighted), Files processed, Field guide, Agent tasks, Rejected by agent.
- Key columns: `needs_review` + `review_reason` (probable errors), `data_caveats` (gaps in the source,
  not errors), `evidence` + `evidence_verified`, `agent_changes` (old -> new with quote), `agent_trace`.
- `outputs/runs/runs.csv`: one row per run with time, calls, tokens and cost per phase (extract, repair, agent).

## 4. Web app

```bash
python -m streamlit run app/streamlit_app.py       # opens http://localhost:8501
```

- Run tab: tick project files, upload files, or search Europe PMC; set models and limits in the sidebar;
  Run / Stop; live log and agent steps. All project files start ticked: untick to test on one paper.
- Review tab: filter flagged records, open one to see its source passage and agent history, edit /
  accept / reject; decisions are saved to `<database>.review.json`; downloads exclude rejected records.
- Do not reload the browser during a run (the run continues, but the progress view is lost).
- Theme: `.streamlit/config.toml` (project palette).

## 5. Compare models

```bash
python compare_models.py
```

Scores every output against the reference set `data/gold/peptide_solubility_gold.csv` (recall,
precision on files marked complete in `data/gold/scope.yaml`, field accuracy) and writes charts and
`comparison.xlsx` to `outputs/comparison/`. Rows with `status = draft` are unconfirmed; say so when
reporting scores. Chart colours are restricted to the project palette (see `PALETTE` in the script).

## 6. Adapt to another subdiscipline

Follow `references/new_profile.md`: copy the profile, rewrite task, instructions, prefilter signals and
schema, run a dry run, then a small paid run on one paper, then build a small reference set.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ValueError: numpy.dtype size changed` | numpy/pandas/pyarrow built against different numpy (often in Anaconda). Use the dedicated venv above. |
| `command not found: streamlit` | Installed into another Python. Use `python -m streamlit ...` with the venv active. |
| `sqlite3.OperationalError: disk I/O error` | Store inside a synced folder. Leave `store.path` empty or point it outside OneDrive/Dropbox. |
| `OPENROUTER_API_KEY is not set` | `.env` missing or misnamed variable. |
| A model call stalls for many minutes | Unstable network; the client retries. Stop with Ctrl+C; the run log still records the run. |
| `reached max_calls_per_run` / `agent.max_calls` | Budget limit reached; partial results are saved. Raise the limit only if the user agrees. |
| 400 errors from a reasoning model in the agent | Leave `agent.temperature` unset (some reasoning models reject it) and raise `agent.max_output_tokens` in `papers.yaml`. |
| Records duplicated across main text and SI | Expected when both report the same values; check `source_file` and `location`. |
| Europe PMC or ChemRxiv search fails | Network blocks the API; supply PDFs locally instead. |
