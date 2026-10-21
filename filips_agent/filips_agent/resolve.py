"""Name -> structure lookups that cross-check Claude's SMILES (RULES.md P3, PLAN.md step 4).

OPSIN parses systematic (IUPAC) names; PubChem knows many materials by trade name or
abbreviation (spiro-OMeTAD, PC71BM, Y6). Answers, including "not found", are cached in
data/cache/structures/, so each name is looked up once.

Structures are compared by their skeleton: heavy atoms and how they are connected, ignoring
bond orders, charges, hydrogens and stereo. PubChem writes fullerenes without double bonds,
and drawings differ in Kekulé form or tautomer, so a stricter key would give false alarms.

Polymers and CXSMILES are not checked: OPSIN can't name them and PubChem stores only monomers.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
from pathlib import Path

from rdkit import Chem

from . import fetch
from .ledger import DATA_DIR
from .structures import canonicalize, is_polymer

CACHE_DIR = DATA_DIR / "cache" / "structures"
OPSIN = "https://www.ebi.ac.uk/opsin/ws/"
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/property/SMILES/JSON"
MAX_NAMES = 4  # names per compound to look up
MAX_HITS = 5   # PubChem can return several compounds for one name
PUBCHEM_GAP = 0.4  # seconds between PubChem calls; it allows at most 5 per second
_last_pubchem = 0.0


def _opsin(name):
    name = name.replace("′", "'").replace("″", "''")  # typographic primes: 2′ -> 2'
    try:
        smiles = fetch._get(OPSIN + urllib.parse.quote(name, safe="") + ".smi").decode().strip()
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):  # not a name OPSIN can parse
            return []
        raise
    return [{"smiles": smiles, "id": "OPSIN"}] if smiles else []


def _pubchem(name):
    global _last_pubchem
    time.sleep(max(0.0, _last_pubchem + PUBCHEM_GAP - time.time()))
    _last_pubchem = time.time()
    try:
        raw = fetch._get(PUBCHEM, data=urllib.parse.urlencode({"name": name}).encode())
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):  # no compound with that name
            return []
        raise
    props = json.loads(raw)["PropertyTable"]["Properties"][:MAX_HITS]
    return [{"smiles": p["SMILES"], "id": f"PubChem CID {p['CID']}"} for p in props if p.get("SMILES")]


SOURCES = {"OPSIN": _opsin, "PubChem": _pubchem}


def lookup(source, name, cache_dir=CACHE_DIR):
    """[{smiles, id}] for a name from one source, [] if unknown, None if the service failed."""
    path = Path(cache_dir) / source / (hashlib.sha1(name.encode("utf-8")).hexdigest()[:16] + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["hits"]
    try:
        hits = SOURCES[source](name)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None  # offline or unexpected answer: unchecked, and not cached
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "hits": hits}), encoding="utf-8")
    return hits


def skeleton(smiles):
    """Canonical SMILES of the heavy-atom graph with all bonds single; None if unreadable."""
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    rw = Chem.RWMol(mol)
    for atom in rw.GetAtoms():
        atom.SetFormalCharge(0)
        atom.SetIsAromatic(False)
        atom.SetNumExplicitHs(0)
        atom.SetNoImplicit(True)
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
    for bond in rw.GetBonds():
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
    return Chem.MolToSmiles(rw, isomericSmiles=False)


def cross_check(smiles, names):
    """Compare a compound's SMILES with what OPSIN and PubChem give for its names.

    Returns (smiles, issues, note). With no SMILES from the paper, a looked-up structure is
    used: from OPSIN (a systematic name) it is accepted, from PubChem it goes to review.
    note is for the comment column: where a looked-up structure came from.
    """
    if smiles and (is_polymer(smiles) or " |" in smiles):
        return smiles, [], None
    hits = []  # (source id, name, smiles, skeleton)
    for name in [n for n in names if n and n.strip()][:MAX_NAMES]:
        for source in SOURCES:
            for hit in lookup(source, name.strip()) or []:
                key = skeleton(hit["smiles"])
                if key:
                    hits.append((hit["id"], name, hit["smiles"], key))
    if not hits:
        return smiles, [], None

    if smiles:
        mine = skeleton(smiles)
        agree = [h for h in hits if h[3] == mine]
        if agree:
            return smiles, [], None
        found = "; ".join(f"{h[0]} for {h[1]!r}" for h in hits[:3])
        return smiles, [f"structure differs from {found} (P3)"], None

    source, name, found, _ = hits[0]
    canon, err = canonicalize(found)
    if err:
        return None, [], None
    issues = []
    if len({h[3] for h in hits}) > 1:
        issues.append(f"lookups give different structures for {name!r}; took {source}'s (P3)")
    if source != "OPSIN":
        issues.append(f"structure not from the paper: taken from {source} for {name!r}; check it (P3)")
    return canon, issues, f"structure from {source} ({name!r})"
