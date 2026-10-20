#!/usr/bin/env python3
"""Stage 1 screen: cheap lexical scoring of every abstract against a topic profile.

Profile: a research-profile .md (see profile_md.py) or the equivalent JSON:
{
  "topic": "...",
  "concepts": [                       # each concept = a facet of the topic
    {"name": "product", "weight": 3, "terms": ["medium-chain fatty acid", "caproate", "chain elongation"]},
    ...
  ],
  "negative": ["clinical trial", "tumor"],   # terms that push a paper down
  "categories_boost": ["microbiology", "bioengineering"]
}
A paper scores per concept hit (title hits count double); papers touching several
concepts rank above papers repeating one term. Top --top papers go to Claude for
stage 2 (reading and judging the abstract).
"""
import argparse, json, re
from profile_md import load as load_profile


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower())


def term_re(t):
    t = re.escape(t.lower()).replace(r"\ ", r"[\s\-]?")
    return re.compile(r"(?<![a-z0-9])" + t, re.I)


def shortlist(raw, prof, top=60):
    """Keyword-score every paper in raw against the profile; return the candidates payload."""
    concepts = [(c["name"], c.get("weight", 1), [term_re(t) for t in c["terms"]]) for c in prof["concepts"]]
    neg = [term_re(t) for t in prof.get("negative", [])]
    boost = {c.lower() for c in prof.get("categories_boost", [])}

    scored = []
    for p in raw["papers"]:
        title, abst = norm(p.get("title")), norm(p.get("abstract"))
        hits, s = {}, 0.0
        for name, w, regs in concepts:
            th = sum(1 for r in regs if r.search(title))
            ah = sum(1 for r in regs if r.search(abst))
            if th or ah:
                hits[name] = th + ah
                s += w * (1 + 0.5 * min(th * 2 + ah - 1, 4))
        if not hits:
            continue
        s *= 1 + 0.35 * (len(hits) - 1)              # reward concept coverage
        if p.get("category", "").lower() in boost:
            s *= 1.2
        s -= 2 * sum(1 for r in neg if r.search(title + " " + abst))
        scored.append((s, hits, p))

    scored.sort(key=lambda x: -x[0])
    keep = [dict(p, prefilter_score=round(s, 2), concept_hits=h) for s, h, p in scored[:top]]
    return {**{k: raw[k] for k in ("server", "start", "end")}, "sources": raw.get("sources", [raw["server"]]),
            "screened": len(raw["papers"]),
            "matched": len(scored), "candidates": keep}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    out = shortlist(json.load(open(a.raw)), load_profile(a.profile), a.top)  # profile: .md (user-editable) or .json
    json.dump(out, open(a.out, "w"), indent=1)
    print(f"screened {out['screened']}, {out['matched']} matched any concept, kept top {len(out['candidates'])} -> {a.out}")


if __name__ == "__main__":
    main()
