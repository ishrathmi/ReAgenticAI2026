---
name: lit-digest
description: Weekly bioRxiv / medRxiv / ChemRxiv literature digest for any research topic. Screens every abstract posted in the last N days (default 7), scores relevance 0-100 with Claude, groups papers into High / Medium (check) / Low, and opens a drag-and-drop board where each paper is a card you file into vaults (Must read, Methods, Discard…), then download as PDFs. Use when the user asks for a literature digest, a weekly paper screen, "what's new on bioRxiv/ChemRxiv about X", to start or open LitVault, or to triage preprints into vaults.
---

# LitVault — weekly literature digest

Two-stage screen: a keyword prefilter over *every* abstract in the window, then Claude reads and scores the shortlist. Results open in a local web board. Each research topic is one Markdown **profile** file that the user owns and edits.

`S` below means this skill's `scripts/` folder (Claude Code gives you the skill's base directory when it loads). Everything is Python 3.9+ standard library, so there is nothing to install.

## Where data lives (the workspace)
`profiles/<topic-slug>.md` (one per topic) and `digests/<date>_<topic-slug>/` (candidates, scores, `board.html`), plus `digests/_cache/` (fetched abstracts and PDFs). The workspace is `$LITVAULT_HOME` if set; else the current folder, if it already has a `profiles/` folder; else `~/LitVault` (created on first use). `scripts/workspace.py` decides this. Tell the user the path the first time.

## Main path: start the board
When the user says "start LitVault", "open my literature board", or sets up a digest for the first time:
1. Start the server in the background and leave it running: `python3 "$S/serve.py"` (options: `--project <folder>`, `--port 8780`). It listens on 127.0.0.1 only and prints the workspace path. If port 8780 is taken, pass another `--port`.
2. Open http://localhost:8780 in the browser pane (or tell the user to open it). With no digests yet, it shows an empty board for a new topic.
3. Explain the flow in two lines. Type keywords in the **Research interest** bar; the topic and description can stay blank, because Claude fills them in. Tick the databases, press **Start literature retrieval**, then drag papers into vaults.

**Start literature retrieval** runs `scripts/pipeline.py`, which does every step below by itself (Claude steps go through headless `claude -p` with no tools). The board shows each step live and opens the new board when done. If `claude -p` can't log in, the run still finishes with keyword scores and an amber warning. Tell the user to run `claude` in a terminal and log in again.

## From chat: "run a digest on X"
1. Find or make the profile. Look in `profiles/` for a matching topic first. For a new topic, write `profiles/<slug>.md` (format below):
   - 3–6 keyword groups, one per facet of the topic (system/organism, product/target, process/method, application), each with a weight (4 = core … 1 = minor) and 10–25 terms. Include synonyms, spelling variants (US/UK, hyphens), abbreviations, and organism, enzyme and pathway names that an on-topic abstract would literally contain.
   - Exclude terms for common false-positive contexts, and Preferred categories (bioRxiv/medRxiv subject areas; ChemRxiv papers count as "chemistry").
   - Description (2–3 sentences: what the user studies, what a relevant paper reports) and Notes for the scorer, both taken from what the user said.
   - Avoid bare generic words that collide with other fields ("residue", "productivity", "CO2", "microbiome", "model"). They flood the shortlist.
   - With an existing profile: move any `### Unsorted keywords` (added from the board) into proper groups, and pad thin groups with synonyms. **Never remove a term the user wrote.** Say in one line what you added or moved.
2. Run it: `python3 "$S/pipeline.py" --profile <workspace>/profiles/<slug>.md` (options: `--days 14`, `--sources biorxiv,chemrxiv`, `--top 40`, `--no-claude`). It prints the board path.
3. Show it. Start the server if it isn't running and open the board URL (`http://localhost:8780/digests/<dir>/board.html`). Summarise: abstracts screened per database → keyword matches → scored, the High papers with one line each, and any Medium worth a look. In a quiet week, say so plainly.

### If the headless scoring step fails, score it yourself
Read `digests/<dir>/candidates.json` (title and full abstract of each candidate). Re-read the profile's Description and Notes for the scorer, which override this rubric where they conflict. Write `scores.json` as `[{"doi", "score", "reason": "one sentence", "tags": [2–3]}]`, then rebuild with `python3 "$S/build_board.py" --candidates … --scores … --profile … --out board.html`.
- **85–100** directly on topic (same system, product, process or question)
- **70–84** core mechanism, organism or method of the topic, different context
- **40–69** adjacent (transferable method, upstream/downstream step, competing approach): worth a human check
- **10–39** shares vocabulary or field only
- **0–9** keyword collision

Never inflate scores to fill the High column, and never invent papers. For more than 60 candidates, split the list across parallel subagents that use the same rubric.

## Profile format (`scripts/profile_md.py`; the board's JS mirrors it)
```markdown
---
topic: Phage therapy against antibiotic-resistant bacteria
sources: biorxiv, chemrxiv          # any of biorxiv, medrxiv, chemrxiv (old files: server:)
days: 7
high: 70                            # score thresholds for the High / Medium columns
medium: 40
vaults: Must read, Methods & protocols, Background, Discard
---
# Phage therapy against antibiotic-resistant bacteria

## Description
What the user studies and what a relevant paper reports.

## Keywords
### Phage biology (weight: 4)
- bacteriophage
- lytic phage, temperate phage       # "," or ";" splits a bullet; "2,3-butanediol" stays whole

## Exclude
- phage display

## Preferred categories
- microbiology

## Notes for the scorer
Free text Claude reads when scoring.
```
The file name follows the topic (`slugify` in `serve.py`/`pipeline.py`), so renaming the topic creates a new profile and keeps the old one. Examples: `examples/*.md`.

## Databases (`scripts/sources.py`)
- **bioRxiv, medRxiv:** the official api.biorxiv.org API. PDFs can be downloaded.
- **ChemRxiv:** fetched from Crossref (DOI prefix 10.26434), because ChemRxiv's own API and pages sit behind a Cloudflare bot check. Never try to get around that check. Abstracts and links work, but PDFs can't be fetched automatically; the zip gets a link instead.
- A database that fails doesn't stop the run; the board notes which one was unreachable. Fetches are cached for 6 hours per database and window. Set `LITVAULT_CONTACT=<email>` to identify yourself to Crossref.

## Board features (`assets/board_template.html`)
- Triage columns High / Medium·check / Low, each card with a score badge, database badge, Claude's reason, tags, the expandable abstract and a personal note. A filter box searches the cards.
- **Research interest** bar plus **Edit profile** (form beside the live Markdown, kept in sync). **Save .md** writes `profiles/<slug>.md` directly when served by `serve.py`; a blank topic or description is filled from the keywords first (`/api/autofill`). The page title follows the topic, and a topic switcher lists every profile.
- Run bar: days window, database checkboxes (at least one) and **Start literature retrieval**, with live progress.
- Vaults: a dock pinned to the bottom. Drag cards in (mouse, or press-and-hold on touch), drop on "+ New vault", or press 1–9 on a hovered card. Rename a vault by clicking its name; ✕ twice deletes it. Long vaults scroll. **Discard all** (Low column) moves visible low cards to Discard. **Delete all** (Discard, two clicks) removes them from the board for good. Undo is available throughout.
- **Download N papers** (vault header): one zip with a folder per vault (except Discard) holding `First author Year - Title.pdf`, plus `README.md` (scores, reasons, notes) and `references.bib`. PDFs are cached in `digests/_cache/pdfs/`.
- Exports: Markdown, BibTeX, JSON. Card placement, notes and vaults are saved per board in the browser (localStorage).

## Weekly automation
Use the `schedule` skill with a prompt like "Run lit-digest with profiles/<slug>.md and summarise the High papers". Point it at the same profile every week so the user's edits carry over.
