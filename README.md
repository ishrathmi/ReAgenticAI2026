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

## Using Claude to run it

The repository includes a Claude skill, `.claude/skills/litagent/`, with setup, run, review, comparison
and troubleshooting instructions. Claude Code loads it automatically when opened in this repository; ask,
for example, "set up the literature agent and do a dry run". To adapt the agent to another
subdiscipline, see `.claude/skills/litagent/references/new_profile.md`.

## Running the agent

Each researcher keeps a **profile** in `config/<profile>/` with two files:

- `papers.yaml` — sources (local folders, search queries), the task description and extraction instructions, prefilter keywords, budget and output settings.
- `schema.json` — the fields to extract. The extraction prompt is built from this file.

```bash
python run_agent.py --profile peptide_solubility --dry-run   # preview; no API calls
python run_agent.py --profile peptide_solubility             # writes outputs/peptide_solubility_db.csv and .xlsx
```

To start your own profile, copy `config/peptide_solubility/` to `config/<your_topic>/`, then edit `task`, `instructions`, `prefilter.signals` and `schema.json`. Set `domain:` only if you add a post-processing module in `src/litagent/domains/` (unit conversion, derived values); otherwise remove the line.

## Review agent (LangGraph)

After the pipeline, the review agent works on the problems its checks flagged. Each problem becomes a task
(for example "these 12 records from SI Table 2 have no unit"), and the model chooses tools until it
accepts the task or reaches the step limit:

| Tool | Purpose |
|---|---|
| `search_paper(query)` | full-text search over the paper and its SI |
| `read_section(section, query)` | e.g. the Methods, for termini, units, salt forms |
| `read_nearby(chunk_id)` | captions, footnotes, tables split across chunks |
| `find_sequence(label)` | sequence tables and passages that name the label |
| `re_extract(chunk_id, instruction)` | extract again from one passage or table (missed rows) |
| `update_record(ids, field, value, quote)` | change a field; the quote must be found in the paper |
| `reject(ids, reason)` / `accept(reason)` | remove wrong records / finish the task |

```bash
python run_agent.py --profile peptide_solubility --agent        # pipeline, then the agent
python compare_models.py                                         # pipeline vs pipeline + agent
```

Settings are under `agent:` in the profile (model, step and call limits, which flags become tasks).
With the agent on, a run writes two outputs: `…__pipeline` and `…__agent-<model>`. The agent's workbook
adds an "Agent tasks" sheet (every task, its tool calls and outcome) and a "Rejected by agent" sheet.
Columns `agent_status`, `agent_changes` (old -> new, with the supporting quote) and `agent_trace`
show what happened to each record.

## Web app (local demo)

```bash
streamlit run app/streamlit_app.py
```

Opens in the browser at http://localhost:8501 and uses the OpenRouter key in `.env`.

- **Run tab:** tick papers from the project folder, upload PDF/XLSX/DOCX files, or search Europe PMC and
  tick results; choose the extraction and agent models in the sidebar; click Run. Progress, the log and
  every agent step update live. Stop ends the run after the current model call.
- **Review tab:** pick an output database, filter flagged records, click a record to see its evidence,
  source passage, repair and agent history; edit, accept or reject it. Decisions are saved next to the
  database (`<name>.review.json`); "Download reviewed CSV/Excel" leaves out rejected records.

Uploads are kept in `~/.cache/litagent/uploads/`, outside the repository.

## Paper store and sources

Every file is parsed once into a paper store (SQLite, default `~/.cache/litagent/papers.sqlite`, kept
outside synced folders on purpose). It holds each paper's files (main text and SI), every passage and
table with its section label, and a full-text index the agent's tools search.

Sources, set in `papers.yaml`:
- `local_folders`: PDF, XLSX and DOCX files you supply (needed for subscription papers).
- `queries` with `source: europepmc`: open-access papers from Europe PMC, downloaded with their full text
  (structured XML with exact sections and tables) and SI files.
- `queries` with `source: chemrxiv`: ChemRxiv preprints.

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
