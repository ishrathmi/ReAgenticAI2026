# LitVault

A [Claude Code](https://claude.com/claude-code) skill that reads the week's preprints for you.

Tell it what you research (a few keywords are enough). Every week it screens **every** new abstract on **bioRxiv**, **medRxiv** and/or **ChemRxiv**, has Claude score each shortlisted paper 0–100 for relevance to *your* topic, and opens a local board where each paper is a card:

- **Triage columns:** High relevance, Medium (worth a check) and Low. Each card shows Claude's one-line reason, tags, the abstract and your own notes.
- **Vaults:** drag cards into *Must read*, *Methods & protocols*, *Background* or your own vaults. **Discard all** clears the Low column in one click.
- **Download:** one zip of PDFs with a folder per vault, plus a README and a `references.bib` for Zotero, Mendeley or EndNote.
- **Your research profile** is a plain Markdown file you can edit in the board or by hand. Claude fills in a blank topic and description from your keywords and suggests synonyms, and never deletes your words.

Works for any field. There is nothing to install beyond Python 3.9+ (standard library only) and Claude Code.

## Install

LitVault lives in the `litvault/` folder of the [ReAgenticAI2026](https://github.com/ishrathmi/ReAgenticAI2026) team repo. Clone the repo, then run the installer, which copies the skill into `~/.claude/skills/lit-digest`:

```bash
git clone https://github.com/ishrathmi/ReAgenticAI2026.git
```
```bash
sh ReAgenticAI2026/litvault/install.sh
```

Restart Claude Code (or start a new session) so it picks up the skill. To update later, run `git pull` in the clone and the installer again; your previous copy is kept as `~/.claude/lit-digest.backup-<date>`. To install into one project only, run the installer from that project's folder with `--project`.

## Use

In Claude Code, say:

> start LitVault

Claude starts a small local server and opens **http://localhost:8780**. Then:

1. Type keywords into the **Research interest** bar, e.g. `bacteriophage, phage therapy, Klebsiella pneumoniae`. You can leave the topic and description blank.
2. Tick the databases to search and set the window (default: the last 7 days).
3. Press **▶ Start literature retrieval**. It takes about a minute: fetch → keyword screen → Claude reads and scores → board.
4. Drag papers into vaults, then press **⬇ Download** for the PDFs.

You can also stay in chat: *"run a lit digest on CRISPR base editing in plants"*, or *"run lit-digest every Monday for my phage profile"* (uses Claude Code's scheduling).

## Where your data goes

Profiles and results are kept in `~/LitVault/` (`profiles/`, `digests/`), or in `$LITVAULT_HOME` if you set it, or in the current folder if it already has a `profiles/` folder. Nothing leaves your machine except:

- the public preprint APIs (api.biorxiv.org, api.crossref.org) and PDF downloads from biorxiv.org / medrxiv.org;
- the paper titles and abstracts, and your profile, which are sent to Claude through your own Claude Code login for scoring.

The server listens on `127.0.0.1` only.

## Good to know

- **ChemRxiv** results come from Crossref, because ChemRxiv's own site blocks automated access. Abstracts and links work, but PDFs can't be downloaded automatically; the zip includes a link to open instead.
- If Claude can't be reached (for example, an expired login), the run still finishes with keyword-only scores and the board says so. Run `claude` in a terminal and log in again.
- Set `LITVAULT_CONTACT=you@example.org` to identify yourself to Crossref's API (recommended by Crossref).
- `skills/lit-digest/examples/` has two example profiles: phage therapy, and organic waste → medium-chain fatty acids.

## Files

```
.claude-plugin/          plugin + marketplace manifests
skills/lit-digest/
  SKILL.md               instructions Claude follows
  scripts/serve.py       local web app + API (save profiles, run retrievals, zip downloads)
  scripts/pipeline.py    refine keywords → fetch → screen → Claude scoring → board
  scripts/sources.py     bioRxiv / medRxiv / ChemRxiv fetchers
  scripts/…              profile format, keyword screen, board builder, PDF zip
  assets/board_template.html   the board UI (single file, no build step)
  examples/              sample research profiles
install.sh               copy-install without the plugin system
```

## License

MIT
