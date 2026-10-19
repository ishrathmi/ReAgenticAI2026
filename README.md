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

## Repository structure

```
src/litagent/
  models.py        OpenRouter adapter and FakeModel for tests
  config.py        loads config/papers.yaml and config/schema.json
  agent.py         agent loop (to be built)
  tools/           search, fetch, parse_pdf, extract
config/
  papers.yaml      DOIs or search queries to process
  schema.json      fields to extract (agree on these as a team)
data/sample/       small open-access test papers
tests/             offline tests using FakeModel
examples/          runnable examples
```

To adapt the agent to your own research, edit `config/papers.yaml` and `config/schema.json`; the code stays the same.

## Data sources

Use official APIs (Crossref, OpenAlex, Semantic Scholar, arXiv, ChemRxiv, Europe PMC). Do not scrape publisher websites or commit full-text PDFs.
