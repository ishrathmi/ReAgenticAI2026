#!/usr/bin/env python3
"""Merge candidates + scores and render the drag-and-drop vault board.

scores.json: [{"doi": "...", "score": 0-100, "reason": "one sentence", "tags": ["..."]}, ...]
Tiers: high >= --high, medium >= --medium (defaults from the profile's front matter: 70 / 40), else low.
"""
import argparse, html, json, os
from profile_md import load as load_profile, parse as parse_profile, render as render_profile
from sources import SOURCES, parse_sources

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VAULTS = ["Must read", "Methods & protocols", "Background", "Discard"]


def paper_url(p, server):
    return p.get("url") or f"https://www.{p.get('source') or server}.org/content/{p['doi']}v{p.get('version', 1)}"


def board_data(prof, profile_md, profile_file, cand=None, scores=None, high=None, medium=None, vaults=None, scoring="claude", note=""):
    """Board payload. With cand=None this is an empty board (new topic, nothing retrieved yet)."""
    high = high or int(prof.get("high", 70))
    medium = medium or int(prof.get("medium", 40))
    vaults = vaults or prof.get("vaults") or DEFAULT_VAULTS
    papers = []
    server = (cand or {}).get("server") or ", ".join(SOURCES[s] for s in parse_sources(prof.get("sources") or prof.get("server", "")))
    for p in (cand or {}).get("candidates", []):
        s = (scores or {}).get(p["doi"])
        if not s:
            continue
        sc = int(s["score"])
        papers.append({
            "id": p["doi"], "title": p["title"].strip(), "authors": p.get("authors", ""),
            "corr": p.get("author_corresponding_institution", ""), "date": p.get("date", ""),
            "category": p.get("category", ""), "abstract": p.get("abstract", "").strip(),
            "url": paper_url(p, server), "pdf_url": p.get("pdf_url", ""), "source": p.get("source") or server, "score": sc,
            "tier": "high" if sc >= high else "medium" if sc >= medium else "low",
            "reason": s.get("reason", ""), "tags": s.get("tags", []),
        })
    papers.sort(key=lambda x: -x["score"])
    c = cand or {}
    return {
        "id": f"{prof.get('topic', '')}|{c.get('start', '')}|{c.get('end', '')}",
        "topic": prof.get("topic", ""),
        "start": c.get("start", ""), "end": c.get("end", ""), "server": server,
        "screened": c.get("screened", 0), "matched": c.get("matched", 0), "reviewed": len(papers),
        "empty": cand is None,
        "scoring": scoring, "scoring_note": note,
        "thresholds": {"high": high, "medium": medium},
        "vaults": [v.strip() for v in vaults if v.strip()],
        "profile_md": profile_md, "profile_file": profile_file,
        "serve_cmd": f'python3 "{os.path.join(HERE, "serve.py")}"',
        "papers": papers,
    }


def render_html(data):
    tpl = open(os.path.join(HERE, "..", "assets", "board_template.html")).read()
    page = tpl.replace("/*__DIGEST_DATA__*/null", json.dumps(data).replace("</", "<\\/"))
    title = (data["topic"] + " · " if data["topic"] else "") + "Literature Vault"
    return page.replace("__TITLE__", html.escape(title))


def empty_board(profile_md, profile_file):
    return render_html(board_data(parse_profile(profile_md), profile_md, profile_file))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--scores", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--high", type=int)
    ap.add_argument("--medium", type=int)
    ap.add_argument("--vaults")
    ap.add_argument("--scoring", default="claude", choices=["claude", "keywords"])
    ap.add_argument("--note", default="", help="shown on the board, e.g. why scoring fell back to keywords")
    a = ap.parse_args()

    prof = load_profile(a.profile)
    profile_md = open(a.profile).read() if a.profile.endswith(".md") else render_profile(prof)
    profile_file = os.path.basename(a.profile) if a.profile.endswith(".md") else ""
    scores = {s["doi"]: s for s in json.load(open(a.scores))}
    data = board_data(prof, profile_md, profile_file, json.load(open(a.candidates)), scores,
                      a.high, a.medium, a.vaults.split(",") if a.vaults else None, a.scoring, a.note)
    open(a.out, "w").write(render_html(data))
    n = {t: sum(p["tier"] == t for p in data["papers"]) for t in ("high", "medium", "low")}
    print(f"board -> {a.out}  (high {n['high']}, medium {n['medium']}, low {n['low']})")


if __name__ == "__main__":
    main()
