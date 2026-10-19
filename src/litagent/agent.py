"""The agent loop. To be built by the team.

Planned flow for each paper:
  1. search  - resolve a DOI or query to metadata (tools/search.py)
  2. fetch   - retrieve open-access full text or abstract (tools/fetch.py)
  3. parse   - convert PDF/HTML to plain text (tools/parse_pdf.py)
  4. extract - ask the model for fields defined in config/schema.json (tools/extract.py)
  5. validate - check the result against the schema; retry or flag on failure
"""

from __future__ import annotations


def run(papers_config: dict, schema: dict, model) -> list[dict]:
    raise NotImplementedError("Agent loop not yet implemented.")
