# Agent loop: failure types and planned tools

The pipeline (`src/litagent/agent.py`) runs a fixed first pass. The agent loop will then take each
flagged record or incomplete table and let the model choose the next action from a small tool set,
within a per-record step limit and the run's call budget. Every decision is logged with
`RunLogger.event("agent_step", ...)`, and each tool call is timed with `RunLogger.stage("tool:<name>")`.

## Failure types

Each failure type is detected by code, so the loop starts only where there is a known problem.

| # | Failure type | Seen in | How it is detected now | Planned tool / action |
|---|---|---|---|---|
| 1 | Table only partly extracted (model stops early) | Word SI, J. Mol. Liq. dipeptide tables | `table_coverage` event; `review_reason` "table only X% extracted"; Files sheet `incomplete_tables` | `re_extract(location, rows=…)`: resend the missing rows only |
| 2 | Sequence missing; peptide named by label only | Nat Commun main text ("variant 7") | `review_reason` "sequence not found" | `find_sequence(label)`: search all tables and passages in the paper group, including SI |
| 3 | Termini filled in although not stated in the passage | Nat Commun Table 3 (Methods states C-terminal amides) | `conversion_note` "MW assumes free termini"; termini mismatch with Methods | `read_section("methods")`: look up synthesis details (resin, capping) and correct termini |
| 4 | Unit missing or guessed | Word SI (caption gives no unit) | `review_reason` "unit not stated" | `read_nearby(location)` / `read_section("methods")` for the unit; otherwise keep null |
| 5 | Wrong field content (e.g. counter-ion in `c_terminus`) | Teixobactin paper (`c_terminus` = "TFA") | schema enums and simple rules (planned) | `re_extract(location)` with the specific error stated |
| 6 | Evidence quote not found in the source | 1 record, first run | `evidence_verified` = FALSE | `read_nearby(location)`, then accept with corrected quote or reject |
| 7 | Values only in figures | Nat Commun solubility curves | not detected | out of scope for text models; candidate for a vision model or the Source Data file |
| 8 | Too few records for a topic | — | record count per query | `search_more(query)` (ChemRxiv) |

## Rules for the loop

- Tools may only add information found in the documents; every change must carry a verified quote.
- A record changed by the loop records the tool sequence in a new `agent_trace` column.
- Stop conditions: record accepted or rejected, step limit reached (default 4), or call budget reached.
- Small models choose tools less reliably; invalid tool calls are logged and counted, and the record is left flagged.
