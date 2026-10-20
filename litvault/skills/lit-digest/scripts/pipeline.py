#!/usr/bin/env python3
"""One-shot literature retrieval for a research profile: refine -> fetch -> shortlist -> score -> board.

    python3 pipeline.py --profile <project>/profiles/<name>.md [--days 7] [--sources biorxiv,chemrxiv] [--top 40]

Claude steps run through the headless Claude Code CLI (`claude -p`, no tools). If the CLI is
missing or fails, refining is skipped and scoring falls back to keyword scores; the board says so.
Progress is written to --status as JSON so the local server can show it live.
"""
import argparse, json, os, re, shutil, subprocess, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profile_md import parse, render
from sources import fetch, parse_sources, SOURCES
from prefilter import shortlist
from build_board import board_data, render_html
from workspace import default_workspace, ensure

STEPS = [("refine", "Refine keywords"), ("fetch", "Fetch abstracts"), ("shortlist", "Keyword screen"),
         ("score", "Claude reads & scores"), ("board", "Build board")]
RUBRIC = """Score each paper 0-100 for relevance to the research profile below.
- 85-100: directly on topic (same feedstock/system, product, process or question).
- 70-84: core mechanism, organism or method of the topic, in a different context.
- 40-69: adjacent: transferable method, upstream/downstream step, competing approach. Worth a human check.
- 10-39: shares vocabulary or field only.
- 0-9: keyword collision.
The profile's Description and Notes for the scorer override this rubric where they conflict.
Be honest: in a quiet week most papers are low. Never inflate scores."""


class Status:
    def __init__(self, path):
        self.path, self.data = path, {"started": datetime.now().isoformat(timespec="seconds"), "done": False,
                                      "error": None, "board": None, "log": [],
                                      "steps": [{"id": i, "label": l, "state": "pending", "detail": ""} for i, l in STEPS]}
        self.write()

    def write(self):
        if self.path:
            tmp = self.path + ".tmp"
            json.dump(self.data, open(tmp, "w"))
            os.replace(tmp, self.path)

    def step(self, sid, state, detail=None):
        for s in self.data["steps"]:
            if s["id"] == sid:
                s["state"] = state
                if detail is not None:
                    s["detail"] = detail
        self.write()

    def log(self, msg):
        print(msg, file=sys.stderr)
        self.data["log"] = (self.data["log"] + [msg])[-40:]
        self.write()


def slugify(s):
    """URL/file-safe topic name; long names are cut at a word boundary."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s if len(s) <= 60 else s[:60].rsplit("-", 1)[0]) or "topic"


def claude(prompt, timeout=900):
    """Run headless Claude Code with no tools; return the text result."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI ('claude') not found on PATH")
    r = subprocess.run([exe, "-p", "--tools", "", "--no-session-persistence", "--output-format", "json"],
                       input=prompt, capture_output=True, text=True, timeout=timeout)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError((r.stderr or r.stdout or "no output").strip()[:300])
    if d.get("is_error"):
        raise RuntimeError(str(d.get("result"))[:300])
    return d.get("result", "")


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    text = m.group(1) if m else text
    i, j = min([k for k in (text.find("["), text.find("{")) if k >= 0], default=-1), max(text.rfind("]"), text.rfind("}"))
    return json.loads(text[i:j + 1])


def all_terms(prof):
    return [t for g in prof.get("concepts", []) for t in g["terms"]]


STOPWORDS = set("""a an and are as at be by can collect do does find for from get how i in into is it its
list me my of on or our paper papers please show study that the their them these this those to use used uses
using want we what which who will with all any about new recent research produce produces producing
production make makes making give gives look looking interested interest focus related""".split())


def fallback_terms(text, limit=12):
    """Without Claude: pull keyword-like words out of a sentence (acronyms, long words, hyphenated terms)."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]*[A-Za-z0-9]|[A-Z]{2,}", text or "")
    out = []
    for w in words:
        if w.lower() in STOPWORDS or (len(w) < 4 and not w.isupper()):
            continue
        if w.lower() not in {x.lower() for x in out}:
            out.append(w)
    return out[:limit]


def autofill(prof, use_claude=True):
    """Fill whatever is blank (topic, description, keywords) from whatever the user gave.
    Returns (filled_fields, via); filled_fields may include "concepts"."""
    terms = all_terms(prof)
    have = {"topic": bool((prof.get("topic") or "").strip()), "description": bool((prof.get("description") or "").strip()),
            "keywords": bool(terms)}
    missing = [k for k, v in have.items() if not v]
    if not missing or not any(have.values()):
        return {}, None
    if use_claude:
        groups = "\n".join(f"- {g['name']}: {', '.join(g['terms'])}" for g in prof["concepts"]) or "(none)"
        want = {"topic": '"topic": "4-10 word research-topic title, sentence case, no trailing period (e.g. \\"Phage therapy against antibiotic-resistant bacteria\\")"',
                "description": '"description": "2-3 sentences: what this researcher studies and what a relevant paper reports, so a scorer can tell on-topic papers from merely related ones"',
                "keywords": '"concepts": [{"name": "facet name", "weight": 1-4, "terms": ["..."]}]  (3-5 groups, one per facet of the request, weight 4 = core; 8-20 terms each: synonyms, spelled-out abbreviations AND the abbreviations, organism / enzyme / pathway / product names an on-topic abstract would literally contain; avoid bare generic words like "model", "production", "microbiome")'}
        prompt = f"""A researcher is setting up a weekly preprint literature screen (bioRxiv, medRxiv, ChemRxiv) and left some fields blank.
What they gave:
Topic: {prof.get('topic') or '(blank)'}
Description / request: {prof.get('description') or '(blank)'}
Keywords by group:
{groups}
Notes: {prof.get('notes') or '(none)'}

The request may be phrased as a task (e.g. "collect all the bacteria that use RBO to produce MCFA"); treat it as the research interest and expand abbreviations (RBO = reverse beta-oxidation, MCFA = medium-chain fatty acids).
Write ONLY the blank fields, based only on what they gave; don't invent a narrower focus. Return one JSON object with these keys:
{{{", ".join(want[k] for k in missing)}}}"""
        try:
            out = extract_json(claude(prompt, timeout=240))
            got = {k: str(out.get(k, "")).strip() for k in ("topic", "description") if k in missing and str(out.get(k, "")).strip()}
            if "keywords" in missing:
                cs = [{"name": str(g.get("name") or "Keywords").strip(), "weight": max(1, min(4, int(g.get("weight", 2)))),
                       "terms": list(dict.fromkeys(str(t).strip() for t in g.get("terms", []) if str(t).strip()))}
                      for g in out.get("concepts", []) if isinstance(g, dict)]
                cs = [c for c in cs if c["terms"]]
                if cs:
                    got["concepts"] = cs
            if got:
                return got, "claude"
        except Exception as e:
            print(f"autofill via Claude failed: {e}", file=sys.stderr)
    # fallback without Claude: build the blanks from the words the user gave
    got = {}
    source_text = " ".join(filter(None, [prof.get("topic"), prof.get("description")]))
    if "keywords" in missing:
        kw = fallback_terms(source_text)
        if kw:
            got["concepts"] = [{"name": "Unsorted keywords", "weight": 2, "terms": kw}]
            terms = kw
    if "topic" in missing:
        if have["description"]:
            words = prof["description"].strip().rstrip(".").split()
            t = " ".join(words[:10]) + ("…" if len(words) > 10 else "")
        else:
            head = terms[:3]
            t = ", ".join(head[:-1]) + " and " + head[-1] if len(head) > 1 else head[0]
        got["topic"] = t[0].upper() + t[1:]
    if "description" in missing and terms:
        got["description"] = "Papers about " + ", ".join(terms[:12]) + (", and related topics." if len(terms) > 12 else ".")
    return got, "keywords"


def needs_refine(prof):
    groups = prof["concepts"]
    return (not groups or any(g["name"].lower().startswith("unsorted") for g in groups)
            or sum(len(g["terms"]) for g in groups) < 30 or any(len(g["terms"]) < 6 for g in groups))


def refine(prof, md_text):
    """Ask Claude to sort unsorted keywords and add synonyms. Every user term is kept."""
    prompt = f"""You maintain a research-interest profile used to keyword-screen every new preprint abstract (bioRxiv, medRxiv, ChemRxiv), then score relevance.
Improve the profile below and return it as JSON: {{"concepts":[{{"name":..,"weight":1-4,"terms":[..]}}], "negative":[..], "categories_boost":[..], "added_summary":"one short sentence"}}.
Rules:
- Keep every existing term (you may move terms between groups). Move each term from any "Unsorted keywords" group into the best-fitting group or a new group; drop the Unsorted group.
- 3-6 groups, one per facet of the topic (e.g. system/organism, product/target, process/method, application). Weight 4 = core facet, 1 = minor.
- Bring each group to 10-25 terms: synonyms, spelling variants (US/UK, hyphenation), abbreviations, organism, enzyme and pathway names an on-topic abstract would literally contain.
- Avoid bare generic words that collide with other fields ("residue", "productivity", "CO2", "microbiome", "model").
- negative: 5-12 terms marking common off-topic contexts. categories_boost: likely bioRxiv categories (lowercase names, e.g. "microbiology").

Profile (Markdown):
{md_text}"""
    out = extract_json(claude(prompt))
    old_terms = {g["name"]: g["terms"] for g in prof["concepts"]}
    new = dict(prof, concepts=[{"name": g["name"], "weight": int(g.get("weight", 2)), "terms": list(dict.fromkeys(g["terms"]))}
                               for g in out["concepts"] if g.get("terms")])
    have = {t.lower() for g in new["concepts"] for t in g["terms"]}
    lost = [t for ts in old_terms.values() for t in ts if t.lower() not in have]
    if lost:  # never drop the user's words
        new["concepts"].append({"name": "Unsorted keywords", "weight": 2, "terms": lost})
    for k in ("negative", "categories_boost"):
        new[k] = list(dict.fromkeys(prof.get(k, []) + [t for t in out.get(k, []) if isinstance(t, str)]))
    return new, out.get("added_summary", "")


def score_batch(prof, batch):
    papers = "\n\n".join(f"[{p['doi']}] {p['title']}\nCategory: {p.get('category', '')}\n{p.get('abstract', '')}" for p in batch)
    prompt = f"""{RUBRIC}

Research profile
Topic: {prof['topic']}
Description: {prof.get('description', '')}
Notes for the scorer: {prof.get('notes', '')}
Keyword groups: {'; '.join(g['name'] for g in prof['concepts'])}

Return ONLY a JSON array, one object per paper, in this shape:
[{{"doi": "...", "score": 0-100, "reason": "one sentence on why it matters to this profile, or why not", "tags": ["2-3 short tags"]}}]

Papers:
{papers}"""
    res = extract_json(claude(prompt))
    return {r["doi"]: {"doi": r["doi"], "score": max(0, min(100, int(r["score"]))), "reason": r.get("reason", ""),
                       "tags": r.get("tags", [])[:3]} for r in res if r.get("doi")}


def keyword_scores(cand):
    top = max([p["prefilter_score"] for p in cand["candidates"]] or [1])
    return {p["doi"]: {"doi": p["doi"], "score": round(90 * p["prefilter_score"] / top),
                       "reason": "Keyword match: " + ", ".join(f"{k} ×{v}" for k, v in p["concept_hits"].items()),
                       "tags": list(p["concept_hits"])[:3]} for p in cand["candidates"]}


def run(a):
    st = Status(a.status)
    profile_path = os.path.abspath(a.profile)
    if os.path.basename(os.path.dirname(profile_path)) == "profiles":
        project = os.path.dirname(os.path.dirname(profile_path))
    else:
        # a profile from elsewhere (e.g. examples/): copy it into the workspace and run that copy,
        # so results never land next to the source file or inside the skill folder
        project = ensure(default_workspace())
        dest = os.path.join(project, "profiles", os.path.basename(profile_path))
        if not os.path.exists(dest):
            shutil.copyfile(profile_path, dest)
        print(f"using {dest}", file=sys.stderr)
        profile_path = dest
    md_text = open(profile_path).read()
    prof = parse(md_text)
    have_claude = bool(shutil.which("claude")) and not a.no_claude

    # 0. fill a blank topic / description / keyword list from what the user did give (written back into the .md)
    if not prof.get("topic") or not (prof.get("description") or "").strip() or not all_terms(prof):
        if not (prof.get("topic") or (prof.get("description") or "").strip() or all_terms(prof)):
            raise ValueError("The profile is empty: add keywords, a description or a topic")
        st.step("refine", "running", "Filling in the blanks from what you wrote…")
        got, via = autofill(prof, have_claude)
        prof.update(got)
        st.log(f"autofilled {', '.join(got)} via {via}")
        if "topic" in got:
            # the file is named after the topic: move an untitled profile to its proper name
            new_path = os.path.join(os.path.dirname(profile_path), slugify(prof["topic"]) + ".md")
            n = 2
            while os.path.exists(new_path) and os.path.abspath(new_path) != profile_path:
                new_path = os.path.join(os.path.dirname(profile_path), f"{slugify(prof['topic'])}-{n}.md"); n += 1
            if os.path.abspath(new_path) != profile_path:
                os.remove(profile_path)
                profile_path = os.path.abspath(new_path)
        open(profile_path, "w").write(render(prof))
        md_text = open(profile_path).read()
        st.data["profile"] = os.path.basename(profile_path)[:-3]
        st.data["autofilled"] = {("keywords" if k == "concepts" else k): v for k, v in got.items()}
        st.write()
    days = a.days or int(prof.get("days", 7))
    sources = parse_sources(a.sources or prof.get("sources") or "")
    end = date.today()
    start = end - timedelta(days=days)
    out_dir = os.path.join(project, "digests", f"{end.isoformat()}_{slugify(prof['topic'])}")
    os.makedirs(out_dir, exist_ok=True)

    # 1. refine the profile (writes back into the user's .md)
    if have_claude and needs_refine(prof):
        st.step("refine", "running", "Sorting keywords and adding synonyms…")
        try:
            prof, summary = refine(prof, md_text)
            open(profile_path, "w").write(render(prof))
            filled = " Also filled in " + " and ".join(st.data["autofilled"]) + "." if st.data.get("autofilled") else ""
            st.step("refine", "done", (summary or "Profile updated") + filled)
            st.log(f"refined profile: {summary}")
        except Exception as e:
            st.step("refine", "skipped", f"Skipped: {e}")
            st.log(f"refine failed: {e}")
    else:
        filled = " · filled in " + " and ".join(st.data.get("autofilled", {})) if st.data.get("autofilled") else ""
        st.step("refine", "done" if filled else "skipped",
                ("Profile already detailed" if have_claude else "Claude CLI not available") + filled)

    # 2. fetch (cached per window for a few hours)
    cache_dir = os.path.join(project, "digests", "_cache")
    os.makedirs(cache_dir, exist_ok=True)
    papers, per_source, failed = [], {}, []
    for src in sources:
        label = SOURCES[src]
        cache = os.path.join(cache_dir, f"{src}_{start}_{end}.json")
        try:
            if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 6 * 3600:
                got = json.load(open(cache))["papers"]
            else:
                st.step("fetch", "running", f"{label} {start} → {end}")
                got = fetch(src, start, end, progress=lambda i, n, l=label: st.step("fetch", "running", f"{l}: {i}/{n}"))
                json.dump({"source": src, "start": str(start), "end": str(end), "papers": got}, open(cache, "w"))
            for p in got:  # caches written before multi-source support lack these fields
                p.setdefault("source", src)
                p.setdefault("url", f"https://www.{src}.org/content/{p['doi']}v{p.get('version', 1)}")
            papers += got
            per_source[label] = len(got)
        except Exception as e:
            failed.append(label)
            st.log(f"{label} fetch failed: {e}")
    if not papers:
        raise RuntimeError("Could not fetch any abstracts" + (f" ({', '.join(failed)} failed)" if failed else ""))
    raw = {"server": ", ".join(SOURCES[s] for s in sources), "sources": sources, "start": str(start), "end": str(end), "papers": papers}
    st.step("fetch", "done", " · ".join(f"{k} {v:,}" for k, v in per_source.items()) + " abstracts" +
            (f" · {', '.join(failed)} unavailable" if failed else ""))

    # 3. keyword screen
    st.step("shortlist", "running")
    cand = shortlist(raw, prof, a.top)
    json.dump(cand, open(os.path.join(out_dir, "candidates.json"), "w"), indent=1)
    st.step("shortlist", "done", f"{cand['matched']} matched → top {len(cand['candidates'])}")

    # 4. score
    scoring, note = "claude", ""
    scores = {}
    if have_claude and cand["candidates"]:
        st.step("score", "running", f"0/{len(cand['candidates'])} papers")
        batches = [cand["candidates"][i:i + 15] for i in range(0, len(cand["candidates"]), 15)]
        try:
            with ThreadPoolExecutor(3) as ex:
                for res in ex.map(lambda b: score_batch(prof, b), batches):
                    scores.update(res)
                    st.step("score", "running", f"{len(scores)}/{len(cand['candidates'])} papers")
        except Exception as e:
            st.log(f"Claude scoring failed: {e}")
            note = f"Claude scoring failed ({e}); scores are keyword-based."
        missing = [p for p in cand["candidates"] if p["doi"] not in scores]
        if missing and not note:
            note = f"{len(missing)} papers were not returned by Claude and use keyword scores."
    else:
        note = "Claude CLI not available; scores are keyword-based." if cand["candidates"] else ""
    kw = keyword_scores(cand)
    if not scores:
        scoring = "keywords"
    for d, s in kw.items():
        scores.setdefault(d, s)
    json.dump(list(scores.values()), open(os.path.join(out_dir, "scores.json"), "w"), indent=1)
    st.step("score", "done" if scoring == "claude" else "skipped", note or f"{len(scores)} papers scored")

    # 5. board
    st.step("board", "running")
    rel_profile = os.path.basename(profile_path)
    if failed:
        note = (note + " " if note else "") + f"{', '.join(failed)} could not be reached this run."
    data = board_data(prof, open(profile_path).read(), rel_profile, cand, scores, scoring=scoring, note=note)
    board = os.path.join(out_dir, "board.html")
    open(board, "w").write(render_html(data))
    n = {t: sum(p["tier"] == t for p in data["papers"]) for t in ("high", "medium", "low")}
    st.step("board", "done", f"{n['high']} high · {n['medium']} medium · {n['low']} low")
    st.data.update(done=True, board=os.path.relpath(board, project))
    st.write()
    print(f"board -> {board}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--days", type=int)
    ap.add_argument("--sources", help="comma list of biorxiv, medrxiv, chemrxiv (default: the profile's)")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--status")
    ap.add_argument("--no-claude", action="store_true", help="keyword scoring only")
    a = ap.parse_args()
    try:
        run(a)
    except Exception as e:
        traceback.print_exc()
        if a.status:
            d = json.load(open(a.status)) if os.path.exists(a.status) else {"steps": []}
            for s in d.get("steps", []):
                if s["state"] == "running":
                    s["state"] = "error"
            d.update(done=True, error=str(e))
            json.dump(d, open(a.status, "w"))
        sys.exit(1)


if __name__ == "__main__":
    main()
