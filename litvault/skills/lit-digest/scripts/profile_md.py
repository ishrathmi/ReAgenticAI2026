#!/usr/bin/env python3
"""Research-profile Markdown <-> profile dict.

The .md file is the user-editable source of truth for a digest topic. Format:

    ---
    topic: Organic waste to MCFA upcycling with fermentation
    sources: biorxiv, chemrxiv            <- any of biorxiv, medrxiv, chemrxiv (old files: `server:`)
    days: 7
    high: 70
    medium: 40
    vaults: Must read, Methods & protocols, Background, Discard
    ---
    # Organic waste to MCFA upcycling with fermentation

    ## Description
    Free text: what you work on and what a relevant paper looks like.

    ## Keywords
    ### MCFA product (weight: 4)
    - caproate
    - medium-chain fatty acid, MCFA        <- "," or ";" splits one bullet into several terms

    ## Exclude
    - clinical

    ## Preferred categories
    - microbiology

    ## Notes for the scorer
    Free text the agent reads when judging abstracts.

Weight 1-4 (4 = core). A group heading without a weight gets 2.

CLI:
    profile_md.py to-json  profile.md  profile.json
    profile_md.py from-json profile.json profile.md
"""
import json, re, sys
from datetime import date
from sources import parse_sources

SECTIONS = {"description": "description", "keywords": "concepts", "concepts": "concepts",
            "exclude": "negative", "negative": "negative", "excluded": "negative",
            "preferred categories": "categories_boost", "categories": "categories_boost",
            "notes for the scorer": "notes", "notes": "notes"}
GROUP_RE = re.compile(r"^###\s+(.+?)\s*(?:\((?:weight|importance)\s*:?\s*([1-4])\))?\s*$", re.I)


def split_terms(s):
    # split on ";" or "," but not inside numbers such as "2,3-butanediol"
    return [t.strip() for t in re.split(r";|(?<!\d),|,(?!\d)", s) if t.strip()]


def parse(text):
    prof = {"topic": "", "description": "", "concepts": [], "negative": [], "categories_boost": [], "notes": ""}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        for ln in lines[1:end]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k in ("days", "high", "medium"):
                    prof[k] = int(v) if v.isdigit() else v
                elif k == "vaults":
                    prof[k] = [x.strip() for x in v.split(",") if x.strip()]
                elif k in ("sources", "server"):
                    prof["sources"] = parse_sources(v)
                else:
                    prof[k] = v
        lines = lines[end + 1:]
    sec, group, free = None, None, []

    def flush_free():
        if sec in ("description", "notes") and free:
            prof[sec] = (prof[sec] + "\n" if prof[sec] else "") + "\n".join(free).strip()
        free.clear()

    for ln in lines:
        s = ln.strip()
        if s.startswith("# ") and not prof["topic"]:
            prof["topic"] = s[2:].strip()
        elif s.startswith("## "):
            flush_free()
            sec, group = SECTIONS.get(s[3:].strip().lower()), None
        elif s.startswith("### ") and sec == "concepts":
            m = GROUP_RE.match(s)
            group = {"name": m.group(1).strip(), "weight": int(m.group(2) or 2), "terms": []}
            prof["concepts"].append(group)
        elif re.match(r"^[-*+]\s+", s) and sec in ("concepts", "negative", "categories_boost"):
            terms = split_terms(re.sub(r"^[-*+]\s+", "", s))
            if sec == "concepts":
                if group is None:
                    group = {"name": "Keywords", "weight": 2, "terms": []}
                    prof["concepts"].append(group)
                group["terms"] += terms
            else:
                prof[sec] += terms
        elif sec in ("description", "notes"):
            free.append(ln)
    flush_free()
    prof["concepts"] = [c for c in prof["concepts"] if c["terms"]]
    return prof


def render(prof):
    fm = ["---", f"topic: {prof.get('topic', '')}", "sources: " + ", ".join(parse_sources(prof.get("sources") or prof.get("server", ""))),
          f"days: {prof.get('days', 7)}", f"high: {prof.get('high', 70)}", f"medium: {prof.get('medium', 40)}"]
    if prof.get("vaults"):
        fm.append("vaults: " + ", ".join(prof["vaults"]))
    fm += [f"updated: {date.today().isoformat()}", "---"]
    out = fm + [f"# {prof.get('topic', '')}", "", "## Description", prof.get("description", "").strip(), "", "## Keywords"]
    for c in prof.get("concepts", []):
        out += [f"### {c['name']} (weight: {c.get('weight', 2)})"] + [f"- {t}" for t in c["terms"]] + [""]
    out += ["## Exclude"] + [f"- {t}" for t in prof.get("negative", [])] + [""]
    out += ["## Preferred categories"] + [f"- {t}" for t in prof.get("categories_boost", [])] + [""]
    out += ["## Notes for the scorer", prof.get("notes", "").strip(), ""]
    return "\n".join(out)


def load(path):
    """Load a profile from .md or .json."""
    text = open(path).read()
    return parse(text) if path.endswith(".md") else json.loads(text)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("to-json", "from-json"):
        sys.exit(__doc__)
    cmd, src, dst = sys.argv[1:]
    if cmd == "to-json":
        json.dump(load(src), open(dst, "w"), indent=2, ensure_ascii=False)
    else:
        open(dst, "w").write(render(load(src)))
    print(f"{src} -> {dst}")
