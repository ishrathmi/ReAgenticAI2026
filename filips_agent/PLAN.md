# Plan: OSC energy-level extraction agent

Builds a growing, deduplicated dataset of HOMO/LUMO/IP/EA values for organic semiconductors by reading papers and following their references. The extraction rules are in [RULES.md](RULES.md); rule IDs (A1, P6b, W3…) are cited below.

## Design principles

1. **Code runs the workflow; Claude reads.** The queue, the "evaluate once" rule (W1, W7), name/SMILES conflict checks (W2) and table sorting (W3) are plain Python backed by SQLite. Claude is called only where reading or judgement is needed, and every call returns JSON validated against a schema.
2. **Claude reports raw values; code converts.** Values are extracted exactly as printed (sign, units, Fc anchor, method wording). `normalize.py` applies the sign rules, Fc re-anchoring and HOMO/LUMO → IP/EA relabelling (P2b, P5a, P6b, P6e). These are unit-tested.
3. **Every value carries evidence:** the exact quote, and the page or figure it came from. Code checks that the number really appears in the quote.
4. **Doubtful records are kept, not dropped.** They go to the review table with a reason (W3).

## Agents (Claude calls)

| Agent | Rules | Input → output | Model |
|---|---|---|---|
| **Triage** | A1–A4 | abstract → `{is_osc, novel_osc, energy_data_likely, reason}`; code applies A4 | Sonnet (try Haiku) |
| **Extractor** | P1, P2, P4–P6, P8 | main text + tables (SI only if needed) → compound records with raw values, method wording, method details for `comment`, original vs cited, quote + location | Opus |
| **Figure reader** | P5b, P6f | figure image + caption → values read from an energy-level diagram or CV trace, with a confidence | Opus (vision) |
| **Structure resolver** | P3 | compound label, name, scheme/figure images, candidate SMILES from tools → chosen SMILES/CXSMILES, route, confidence | Opus (vision) |
| **Reference harvester** | P9 | text + reference list → cited works with likely/possible and the sentence that cites them | Sonnet |
| **Critic** | P7c | records + quotes + method text → uncertainty flags and notes | Opus |

## Tools (plain code)

| Module | Job |
|---|---|
| `ledger.py` | SQLite: `sources` (DOI primary key, status, likelihood, found_in, reason), `compounds`, `records`, `review`. Exports `data.csv`, `review.csv`, `irrelevant.csv`, `reading_list.csv`, `wishlist.csv`. |
| `fetch.py` | DOI → title, abstract, references (as DOIs) from **OpenAlex**. Open-access full text via Unpaywall / Europe PMC / bioRxiv. Otherwise → wish list (W6). |
| `parse.py` | PDF → text by section, tables, figure captions, rendered figure/scheme images (pymupdf). Same for SI documents (P8a). |
| `normalize.py` | Sign and unit conversion, Fc re-anchoring to 4.8 eV, method vocabulary incl. `arbitrary`, `n/a` filling. |
| `structures.py` | Candidate SMILES from several routes, RDKit canonicalisation, formula check (P3e). See below. |
| `checks.py` | Quote contains value; plausible ranges (IP ≈ 4–7 eV, EA ≈ 2–4.5 eV, IP > EA, HOMO < LUMO); DFT vs electrochemical gap roughly consistent; W2a/W2b name–SMILES conflicts. |

## Structures (P3): several routes, then compare

1. **Name → structure:** OPSIN (rule-based IUPAC parser, web service or local Java) for systematic names.
2. **Known compounds:** PubChem PUG REST lookup by name or synonym (P3 well-known, e.g. P3HT, PC71BM, Y6, spiro-OMeTAD). The team's own dataset counts as a source once it grows.
3. **Image → structure:** crop the scheme/figure with pymupdf and run an optical structure recognition tool (OCSR): **DECIMER** (open source, best general accuracy) with **MolScribe** as a second opinion. Both have heavy ML dependencies (TensorFlow / PyTorch), so they're an optional install.
4. **Vision LLM:** the structure resolver also reads the image directly.
5. **Decide:** canonicalise all candidates with RDKit and compare InChIKeys. If the routes agree, accept. If they disagree, the resolver checks plausibility against the image, name and formula. Still unsure → review (P3c). Small molecules always get the formula check (P3e).

Polymers: `*` end markers on the repeat unit. Ambiguous regiochemistry: CXSMILES (P3d).

**RDKit caveats for these two cases:**
- `MolToCXSmiles` silently drops `|m:…|` multicentre blocks. Canonicalise the SMILES part, remap the `m:` atom indices to the new atom order, and re-append the block.
- InChI/InChIKey fails on `*` atoms (it returns an empty key), so polymers and CXSMILES can't be compared by InChIKey. Compare canonical (CX)SMILES strings instead.

## Pipeline

```
seed DOIs (LitVault digest, manual list)
  └─> ledger: skip if already seen (W7)
       └─> Triage (A1–A4) ──> irrelevant.csv
            └─> reading list
                 └─> fetch full text ──(fails)──> wishlist.csv + awaiting_pdf
                      └─> parse → Extractor (main text, then SI) → Figure reader if needed
                           └─> Structure resolver → normalize → checks → Critic
                                └─> each compound → data.csv or review.csv (W3)
                                └─> Reference harvester (P9)
                                     ├─ likely   → reading list
                                     └─ possible → Triage on abstract
```

**Following references.** There's no depth limit. The ledger makes the search converge, because no source is evaluated twice. The limit is per run, for cost control and for checking quality early: a `--batch N` option (default 10) sets how many papers are read in full each run, *likely* sources are read before *possible* ones, and the run can resume from the ledger at any time.

## Paper access (W6)

- **Wish list + inbox (built first):** `data/wishlist.csv` lists what's wanted. Put PDFs in `data/inbox/` (any file name; the DOI is matched from the PDF's metadata or first page, or name it `<doi with / replaced by _>.pdf`). SI files go in the same folder with an `_SI` suffix. The next run picks them up.
- **Browser helper (optional, later):** a Claude Code skill that works through the wish list with Claude in Chrome, using your own browser login to download PDFs into the inbox. It runs interactively in Claude Code, not inside the Python pipeline. Keep downloads modest and personal, in line with publisher terms.

## Folder layout

```
filips_agent/
  RULES.md  PLAN.md  requirements.txt
  osc_extract/          Python package
    ledger.py fetch.py parse.py normalize.py structures.py checks.py run.py
    agents/   triage.py extractor.py figures.py structure.py references.py critic.py
    schemas/  JSON schema per agent output
  eval/
    gold_labels.csv     hand-labelled reference data (tracked)
    score.py            compares a run against gold_labels.csv
  tests/
  data/                 gitignored: inbox/, cache/, ledger.db, output CSVs
  .venv/                gitignored
```

## Build order

1. `normalize.py` + tests: settles the conversion rules.
2. `ledger.py` + `fetch.py` (OpenAlex, Unpaywall) + wish list/inbox.
3. Triage and Extractor agents with schemas, scored against `eval/gold_labels.csv`.
4. `structures.py`: OPSIN, PubChem, RDKit and formula check first; DECIMER/MolScribe after.
5. Figure reader, reference harvester, critic, review tables.
6. Evaluation for Milestone IV: per-field accuracy (SMILES by InChIKey (canonical CXSMILES for polymers), values within ±0.02 eV, methods exact match), 5 runs per paper, baseline vs agent, Opus vs Sonnet.

## Gold labels

`eval/gold_labels.csv` uses the W2 columns. Still to do:
- fill in `name` for each row;
- add cases that cover the tricky rules: Fc anchored at 5.1 or 4.4 eV, a value cited from another paper (should not be extracted), a compound known only by number, values only in a figure, `CV w/o ref`, `arbitrary`.
