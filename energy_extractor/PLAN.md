# Plan: OSC energy-level extraction agent

Builds a growing, deduplicated dataset of HOMO/LUMO/IP/EA values for organic semiconductors by reading papers and following their references. The extraction rules are in [RULES.md](RULES.md); rule IDs (A1, P6b, W3…) are cited below.

## Design principles

1. **Code runs the workflow; Claude reads.** The queue, the "evaluate once" rule (W1, W7), name/SMILES conflict checks (W2) and table sorting (W3) are plain Python backed by SQLite. Claude is called only where reading or judgement is needed, and every call returns JSON validated against a schema.
2. **Claude reports raw values; code converts.** Values are extracted exactly as printed (sign, units, Fc anchor, method wording). `normalize_values.py` applies the sign rules, Fc re-anchoring and HOMO/LUMO → IP/EA relabelling (P2b, P5a, P6b, P6e), and puts each measurement set of a compound on its own row (W2c). These are unit-tested.
3. **Every value carries evidence:** the exact quote, and the page or figure it came from. Code checks that the number really appears in the quote.
4. **Doubtful records are kept, not dropped.** They go to the review table with a reason (W3).

## Agents (Claude calls)

| Agent | Rules | Input → output | Model |
|---|---|---|---|
| **Inbox sorter** *(built)* | W6 | first pages of a dropped file + wanted papers → `{doi, kind: main/si/other}`; only filed when it agrees with the filename/DOI rules | Haiku 5.5 |
| **Triage** *(built)* | A1–A4 | abstract → `{is_osc, novel_osc, energy_data_likely, reason}`; code applies A4 | Opus 5.5 until the triage trial (`eval/triage_trial.py`) decides |
| **Extractor** *(built)* | P1–P9 | the PDF itself (text, tables **and figures**; SI only in a second pass if needed) + OpenAlex reference list → compounds with SMILES, raw values, measurement set, method wording, CV reference, method details, original vs cited, quote + location; uncertain-paper note; P9 references with DOIs | Opus 5.5 |
| **Structure resolver** | P3 | compound label, name, scheme/figure images, candidate SMILES from tools → chosen SMILES/CXSMILES, route, confidence | Opus (vision) |
| **Critic** | P7c | records + quotes + method text → uncertainty flags and notes | Opus |

Because Claude reads PDFs directly (pages as text and images), the planned separate *figure reader* (P5b, P6f) and *reference harvester* (P9) are part of the extractor call for now. They can be split out again if the evaluation shows they need their own pass.

## Tools (plain code)

| Module | Job |
|---|---|
| `ledger.py` | SQLite: `sources` (DOI primary key, status, likelihood, found_in, reason), `compounds`, `records`, `review`. Exports `data.csv`, `review.csv`, `irrelevant.csv`, `reading_list.csv`, `wishlist.csv`. |
| `fetch.py` | DOI → title, abstract, references (DOI + title) from **OpenAlex**; open-access PDF download; otherwise → wish list (W6). Inbox filing with filename/DOI rules + the Haiku sorter. |
| `llm.py` | The only place that calls Claude: structured JSON output validated against a schema, refusal handling, prompt caching, and cost of every call logged to `data/logs/usage.jsonl`. |
| `normalize_values.py` | Sign and unit conversion, one row per measurement set, Fc re-anchoring to 4.8 eV, method vocabulary incl. `arbitrary`, `n/a` filling. |
| `structures.py` | RDKit canonicalisation (keeps and renumbers CXSMILES `m:` blocks), formula check incl. HRMS ions (P3e). |
| `resolve.py` | Looks up each compound's names in OPSIN and PubChem (answers cached in `data/cache/structures/`) and compares them with Claude's as RDKit canonical SMILES without stereo. A disagreement → review; a missing structure is filled in (from PubChem only via review). Polymers and CXSMILES are skipped. |
| `review.py` | The human route: decisions typed into `review.csv` (`accept`, with or without edited cells; `reject`; `reread` with a note that is given to the extractor next time). Edits validated, paper status recomputed, W2a names synced, decisions kept when a paper is read again. |
| `figures.py` + `ocsr.py` | Image check of structures: find the compound's figure (caption naming it or showing structures, scheme final product, or where the extractor said it is drawn), cut out the drawings (DECIMER Segmentation), match each to the label next to it (text layer or OCR), read it with DECIMER and MolScribe, confirm with the formula and OPSIN/PubChem, compare with the extractor's SMILES. Report only: `structure_checks.csv` and a note in the review reason. Tools in their own environments, see `ocsr/README.md`. |
| `checks.py` | Usual ranges (IP 4–7, EA 2–4.5, DFT HOMO −7.5 to −3.5, DFT LUMO −5 to 0 eV), IP > EA, HOMO < LUMO, DFT gap within 1 eV of the measured gap; W2a/W2b name–SMILES conflicts with the data table from other papers. Anything flagged → review. (The quote check is in the extractor.) |

## Structures (P3): several routes, then compare

1. **Name → structure** *(built)*: OPSIN (rule-based IUPAC parser, web service) for systematic names.
2. **Known compounds** *(built)*: PubChem PUG REST lookup by name or synonym (P3 well-known, e.g. P3HT, PC71BM, Y6, spiro-OMeTAD). The team's own dataset counts as a source once it grows.
3. **Image → structure** *(built)*: a fallback for compounds sent to review for a structure reason (no structure, P3c, P3e, invalid SMILES, lookups disagree). It runs right after the paper is extracted, if the image tools are installed, and adds its result to the review reason; it never moves a record out of review. `python -m energy_extractor images` redoes it by hand (`--refresh` reads the drawings again, `--all` checks every compound). On the gold set it only confirmed structures the other routes already had, and couldn't read the cases they missed (fullerene, figures drawing only fragments). Find the figure that shows the compound (caption, scheme final product, or the extractor's `structure_location`), single out its drawing by the label next to it, and read it with **DECIMER** and **MolScribe** (second opinion). The reading must fit the reported formula / OPSIN / PubChem before it counts; a drawing that doesn't is reported as "different compound?". Heavy ML dependencies (TensorFlow / PyTorch), so they're an optional install in separate environments. Labels inside raster figures rely on OCR, so small or rotated labels can be missed (then all drawings in the figure are listed as candidates).
4. **Vision LLM:** the structure resolver also reads the image directly.
5. **Decide:** canonicalise all candidates with RDKit and compare them. *Built for routes 1–2:* comparison as RDKit canonical SMILES without stereo (E/Z and R/S are often drawn differently). Bond orders and charges must match, so unusual drawings such as PubChem's fullerenes (no double bonds) go to review. If the routes agree, accept. If they disagree, the resolver checks plausibility against the image, name and formula. Still unsure → review (P3c). Small molecules always get the formula check (P3e).

Polymers: `*` end markers on the repeat unit. Ambiguous regiochemistry: CXSMILES (P3d).

**RDKit caveats for these two cases:**
- `MolToCXSmiles` silently drops `|m:…|` multicentre blocks. Canonicalise the SMILES part, remap the `m:` atom indices to the new atom order, and re-append the block. Identical fragments (e.g. two `*C` groups) can swap places, so their attachment sets are listed in sorted order to make the result unique (`structures.canonicalize`).
- InChI/InChIKey fails on `*` atoms (it returns an empty key), so polymers and CXSMILES can't be compared by InChIKey. Compare canonical (CX)SMILES strings instead.

## Pipeline

```
seed DOIs (LitVault digest, manual list)
  └─> ledger: skip if already seen (W7)
       └─> Triage (A1–A4) ──> irrelevant.csv
            └─> reading list
                 └─> fetch full text ──(fails)──> wishlist.csv + awaiting_pdf
                      └─> Extractor reads the PDF (main text, then SI if needed)
                           └─> structures (RDKit) → OPSIN/PubChem cross-check → normalize_values → checks (→ later: Structure resolver, Critic)
                                └─> each compound → data.csv or review.csv (W3)
                                     └─> structure-flagged records, if installed: image check (DECIMER + MolScribe) → note in review reason, structure_checks.csv
                                     └─> person decides in review.csv → data / rejected / reread with a note
                                └─> P9 references from the same call
                                     ├─ likely   → reading list
                                     └─ possible → Triage on abstract
```

**Following references.** There's no depth limit. The ledger makes the search converge, because no source is evaluated twice. The limit is per run, for cost control and for checking quality early: a `--batch N` option (default 10) sets how many papers are read in full each run, *likely* sources are read before *possible* ones, and the run can resume from the ledger at any time.

## Paper access (W6)

- **Saved extractions:** every raw answer is kept in `data/extractions/`; `python -m energy_extractor reprocess` rebuilds the tables from them for free after the checks or conversions change.
- **Wish list + inbox (built first):** `data/wishlist.csv` lists what's wanted. Put PDFs in `data/inbox/` (any file name; the DOI is matched from the PDF's metadata or first page, or name it `<doi with / replaced by _>.pdf`). SI files go in the same folder with an `_SI` suffix. The next run picks them up.
- **Browser helper (optional, later):** a Claude Code skill that works through the wish list with Claude in Chrome, using your own browser login to download PDFs into the inbox. It runs interactively in Claude Code, not inside the Python pipeline. Keep downloads modest and personal, in line with publisher terms.

## Folder layout

```
energy_extractor/
  RULES.md  PLAN.md  requirements.txt
  docs/                     workflow-overview.png, workflow-detailed.png
  energy_extractor/         Python package (python -m energy_extractor …)
    pipeline.py             the steps, shared by the command line and the page
    ledger.py fetch.py llm.py normalize_values.py structures.py resolve.py checks.py review.py
    figures.py ocsr.py dois.py web.py __main__.py
    agents/                 inbox_sorter.py triage.py extractor.py (schemas live next to each agent)
    web/                    index.html, the browser page
  ocsr/                     image-tool workers + install notes (run in .venv-decimer / .venv-molscribe)
  eval/
    gold_labels.csv         hand-labelled reference data (tracked)
    score.py                compares stored results with gold_labels.csv (python -m eval.score)
    triage_trial.py         runs triage with several models side by side
  tests/
  data/                     gitignored: inbox/, pdfs/, cache/, ledger.db, extractions/, output CSVs, logs
  .venv/                    gitignored
```

## Build order

1. `normalize_values.py` + tests: settles the conversion rules. *(done)*
2. `ledger.py` + `fetch.py` (OpenAlex) + wish list/inbox, with a small command line (`python -m energy_extractor …`). *(done)*
3. Inbox sorter, triage and extractor agents with schemas, scored against `eval/gold_labels.csv`. *(built; first gold-set run and triage trial in progress)*
4. `structures.py` + `resolve.py` + `checks.py`: RDKit, formula check, OPSIN and PubChem cross-checks, range and W2a/W2b checks. *(done)* DECIMER/MolScribe image check *(built)*; human review route *(built)*.
5. Figure reader, reference harvester, critic, review tables.
6. Evaluation for Milestone IV: per-field accuracy (SMILES by InChIKey (canonical CXSMILES for polymers), values within ±0.02 eV, methods exact match), 5 runs per paper, baseline vs agent, Opus vs Sonnet.

Later:
- Extract the optical gap (`eopt`) as its own value where the paper reports one, with its method (absorption onset, film/solution). EA is not derived from IP − Eg unless the paper states the EA itself (decided 2026-10-21, PF8Cz in 10.1038/s41566-022-00999-9).

## Gold labels

`eval/gold_labels.csv` uses the W2 columns, with names filled in. Still to do:
- add cases that cover the tricky rules: Fc anchored at 5.1 or 4.4 eV, a value cited from another paper (should not be extracted), a compound known only by number, values only in a figure, `CV w/o ref`, `arbitrary`.
