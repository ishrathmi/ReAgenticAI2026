# Extraction rules: organic semiconductor energy levels

What the agent extracts and how it decides. Rules are numbered so that code, prompts and review notes can cite them (e.g. "P6b"). Abstract rules are `A1`–`A4`, paper rules `P1`–`P9`, workflow rules `W1`–`W7`.

## From each abstract

**A1.** Judge whether the paper is about organic semiconductors. If it is not, report it as irrelevant: *"Not organic semiconductors"*.

**A2.** Judge whether the paper contains information about a **novel** organic semiconductor: a conjugated organic compound with semiconductor properties and electronic applications that is synthesised or reported for the first time. If it does, report it as relevant.

**A3.** Judge whether the paper contains information about an organic semiconductor (new or not) and is likely to contain HOMO/LUMO/IP/EA data. If it does, report it as relevant.

**A4.** If neither A2 nor A3 is fulfilled, mark the paper as irrelevant: *"No energy data expected"*.

## From each paper

**P1.** Confirm that organic semiconductors are discussed and investigated.

**P2.** Look for any reported energies of the highest occupied molecular orbital (HOMO), lowest unoccupied molecular orbital (LUMO), ionization potential (IP, I.P.) and electron affinity (EA, E.A.) of any reported organic semiconductor.
- **P2a.** Data must be original to the paper, not imported or copied from another source.
- **P2b.** IP and EA are sometimes called HOMO and LUMO. HOMO/LUMO values obtained through physical measurements are reported as IP/EA.

**P3.** If original HOMO/LUMO/IP/EA data is reported, find the compound it belongs to. The compound is usually named by a full structural name, a number or an abbreviation, and its structure is given by the structural name or in an image. Generate a SMILES string from the name or image and canonicalise it with RDKit.
- **P3a.** If SMILES cannot describe the structure, use CXSMILES (see P3d).
- **P3b.** A well-known compound may be referred to only by a name or abbreviation. In that case, look up the structure online (PubChem).
- **P3c.** If the SMILES is ambiguous, mark it for human review.
- **P3d.** Notation conventions:
  - **Polymers** (homopolymers and alternating copolymers) are written as one repeat unit with `*` end markers, e.g. P3HT `*c1sc(*)c(CCCCCC)c1`. Side chains are written in full.
  - **Ambiguous regiochemistry** (isomer mixtures, substituents at unspecified positions) uses CXSMILES, e.g. multicentre attachment `|m:…|`.
- **P3e.** Formula check (small molecules only, not polymers): if the paper reports a molecular formula, HRMS/MALDI mass or elemental analysis, compare it with the formula and mass RDKit calculates from the SMILES (allowing for adducts such as [M+H]⁺). If they disagree, mark the compound for human review.

**P4.** If the compound is named other than by a number (e.g. *1*, *1c*), report the name. It may be a full structural name (e.g. "Poly(3-hexylthiophene-2,5-diyl)"), a descriptive name (e.g. "Fullerene") or an abbreviation (e.g. "P(NDI2OD-T2)", "Y6", "PBQx-TF").
- **P4a.** If several names are used, report all of them as synonyms, separated by commas.

**P5.** For HOMO/LUMO obtained computationally (density functional theory, DFT), report the computational method: functional and basis set, e.g. B3LYP/6-31G(d,p). Also include any other important method information, likely to be unusual.
- **P5a.** Report HOMO/LUMO as negative values in eV.
- **P5b.** If DFT is discussed but values are only shown in a figure, read figure captions to find the relevant figure, then read the values from it.

**P6.** For IP/EA values, report the method used to obtain them.
- **P6a.** Cyclic voltammetry with ferrocene as internal reference or calibration: `CV`. CV referenced to an electrode (e.g. Ag/Ag⁺) without ferrocene: `CV w/o Fc`.
- **P6b.** IP/EA from CV with a ferrocene reference are reported with the Fc/Fc⁺ couple at **4.8 eV**. If the paper anchors ferrocene at another value (e.g. 4.4 or 5.1 eV), adjust the values: `value_reported + (4.8 − anchor_used)`. If the reference is not stated, use `CV w/o ref`.
- **P6c.** Other methods are reported by name, e.g. `UPS`, `IPES`, `PESA`.
- **P6d.** If a value is derived from a measurement plus the optical gap (Eg), e.g. LUMO = HOMO + Eg, note it in the method of the derived value: `CV + Eg`, `UPS + Eg`.
- **P6e.** Report IP/EA as positive values in eV (convert if reported as negative HOMO/LUMO).
- **P6f.** If the measurement is discussed but values are not in the text, read figure captions to find figures that may contain the data and extract it from the figure. If uncertain, mark for human review.
- **P6g.** If a value is reported but no method is stated, report the method as `arbitrary`.
- **P6h.** Record method details that affect the value in the `comment` field: potential type (onset / half-wave / peak), sample state (film / solution), solvent and electrolyte, the ferrocene anchor the paper used and any adjustment made (e.g. "CV onset, film, 0.1 M Bu4NPF6/MeCN; paper used Fc = 5.1 eV, adjusted by −0.3 eV").

Allowed values for `ip_method` / `ea_method`: `CV`, `CV w/o Fc`, `CV w/o ref`, `UPS`, `IPES`, `PESA`, other named techniques, any of these with ` + Eg`, and `arbitrary`.

**P7.** A compound record needs at least:
- a compound structure, and
- at least one of HOMO, LUMO, IP or EA.

Sub-rules:
- **P7a.** If no compound in the paper meets this, mark the paper as irrelevant.
- **P7b.** Report any missing data as `n/a`.
- **P7c.** If there is ambiguity about methods, high uncertainty in the values, or any other reason to read the paper with great scepticism (e.g. highly anomalous values, methodological problems, conditions that make the values invalid as HOMO/LUMO/IP/EA energies), write a brief note why and mark the paper as **uncertain**. All its compounds then go to the review table with that note.

**P8.** If the information is not in the main text, search the supplementary information (SI). Always read the main text first.
- **P8a.** Only search SI provided as documents (PDF, DOCX, text), not large datasets, videos, etc.

**P9.** List other sources the paper cites in a way that suggests they may contain HOMO/LUMO/IP/EA data on an organic semiconductor. Report the DOI of each, marked **likely** (data specifically attributed to it) or **possible** (mentioned in passing).

## Workflow

**W1. Reading list.** Sources marked *likely* go straight onto the reading list. Sources marked *possible* have their abstracts evaluated one at a time (A1–A4) and are sorted onto the reading list or the irrelevant list. Once a source has been evaluated, it is never evaluated again.

**W2. Data table.** For each item on the reading list, one at a time, collect compound records in a table with the columns:
`name,smiles,homo_dft,lumo_dft,dft_method,ip,ip_method,ea,ea_method,comment,source_doi`
- **W2a.** If the SMILES has already been reported, check that the names agree. If they don't, mark for human review. After review, sync names so all synonyms are reported (e.g. "P(NDI2OD-T2)" and "PNDI2OD-T2" become "PNDI2OD-T2, P(NDI2OD-T2)").
- **W2b.** If the name has already been reported, check that the SMILES agree. If they don't, mark for human review.

**W3. Sorting is per compound, status is per paper.** Each compound record is sorted on its own: complete and unflagged records go to the **data** table, flagged ones (P3c, P3e, P6f, P7c, W2a, W2b) go to the **review** table with the reason. A paper's status follows from its compounds:

| Paper status | Meaning |
|---|---|
| `done` | every compound record is in the data table |
| `partial` | some records in data, some in review |
| `review` | every record in review, including all papers marked uncertain (P7c) |
| `irrelevant` | rejected at the abstract (A1/A4) or paper stage (P1/P7a) |
| `awaiting_pdf` | relevant, but the full text could not be obtained (see W6) |

**W4. Separate tables** for useful data, records needing human review, and irrelevant papers.

**W5. Missing data** is marked `n/a`.

**W6. Paper access.** If the full text can't be retrieved automatically, add the paper to the **wish list** (DOI, title, link, why it's wanted) and set its status to `awaiting_pdf`. When a PDF (and optionally SI) is placed in the inbox folder, the paper re-enters the queue automatically.

**W7. Ledger.** Every source that has ever been added to a reading list or judged irrelevant is kept in a ledger. No source is evaluated more than once.
