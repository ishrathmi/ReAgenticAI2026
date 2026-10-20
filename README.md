# ReAgenticAI2026

Team repository for the **Fall 2026 Agentic AI Workshop**.

## Goal

Build a literature-scraping agent for chemistry that individual researchers can adapt to their own needs with a few modifications.

## Team

| Name | GitHub |
|------|--------|
| Anthony | [@apembere](https://github.com/apembere) |
| Filip | [@filipanies](https://github.com/filipanies) |
| Charlie | [@XcharlieXchen](https://github.com/XcharlieXchen) |
| Ellis | [@ellisrae](https://github.com/ellisrae) |
| Ishrath | [@ishrathmi](https://github.com/ishrathmi) |

## Setup

Requires Python 3.10 or newer.

```bash
git clone https://github.com/ishrathmi/ReAgenticAI2026.git
cd ReAgenticAI2026
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then add your own OPENROUTER_API_KEY
python -m pytest                 # offline checks, no key needed
```

Each team member uses their own OpenRouter key in `.env`. Never commit `.env`.

## Running the agent

Each researcher keeps a **profile** in `config/<profile>/` with two files:

- `papers.yaml` — sources (local folders, search queries), the task description and extraction instructions, prefilter keywords, budget and output settings.
- `schema.json` — the fields to extract. The extraction prompt is built from this file.

```bash
python run_agent.py --profile peptide_solubility --dry-run   # preview; no API calls
python run_agent.py --profile peptide_solubility             # writes outputs/peptide_solubility_db.csv and .xlsx
```

To start your own profile, copy `config/peptide_solubility/` to `config/<your_topic>/`, then edit `task`, `instructions`, `prefilter.signals` and `schema.json`. Set `domain:` only if you add a post-processing module in `src/litagent/domains/` (unit conversion, derived values); otherwise remove the line.

## Comparing models

```bash
python run_agent.py --profile peptide_solubility --model anthropic/claude-sonnet-5.5
python run_agent.py --profile peptide_solubility --model openai/gpt-6.1-sol
python compare_models.py            # charts and workbook in outputs/comparison/
```

`compare_models.py` scores every model output against the reference set in `data/gold/`
(recall, precision, field accuracy), runs the automatic checks, measures agreement between
models, and reports cost and run time. `summary.png` shows all of it on one page.

## Repository structure

```
run_agent.py       command-line entry point
src/litagent/
  models.py        OpenRouter adapter, FakeModel for tests, CachedModel (disk cache + call budget)
  config.py        loads config/<profile>/papers.yaml and schema.json
  agent.py         agent loop: gather -> parse -> filter -> extract -> verify -> link -> enrich
  output.py        CSV and formatted Excel output
  tools/
    search.py      ChemRxiv search (add other APIs here)
    fetch.py       downloads open-access PDFs
    parse_pdf.py   PDF, XLSX and DOCX to text and table chunks
    prefilter.py   keyword scoring to limit what is sent to the model
    extract.py     schema-driven extraction with evidence verification
  domains/         optional per-topic post-processing (e.g. peptide_solubility.py)
config/
  papers.yaml, schema.json   shared defaults
  peptide_solubility/        Ishrath's profile
data/sample/       small open-access test papers
tests/             offline tests using fake models
examples/          runnable examples
```

## Data sources

Use official APIs (Crossref, OpenAlex, Semantic Scholar, arXiv, ChemRxiv, Europe PMC). Do not scrape publisher websites or commit full-text PDFs.
