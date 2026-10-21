"""Browser page for the pipeline: `python -m filips_agent web`, then http://127.0.0.1:8000.

It only listens on this computer. The page shows the ledger's tables, takes dropped files
into data/inbox/, records review decisions, and runs pipeline steps as one background job
at a time, showing what they print as the job log. Steps that call Claude cost money and
only start from a button that shows the estimate.
"""
import sys
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from rdkit import Chem
from rdkit.Chem.Draw import rdMolDraw2D
from werkzeug.utils import secure_filename

from . import fetch, llm, ocsr, pipeline, review
from .dois import clean_doi, doi_slug
from .ledger import DATA_DIR, STATUSES, TO_READ, TO_TRIAGE, WISHED, Ledger

PAGE = Path(__file__).with_name("web") / "index.html"
OUTPUT = DATA_DIR / "output"
# for the estimates on the buttons: gold run and triage trial of 2026-10-21
COST_PER_PAPER = 0.45
COST_PER_ABSTRACT = 0.005


class _ThreadOutput:
    """sys.stdout that also sends what a job's thread prints to that job's log."""

    def __init__(self, stream):
        self.stream, self.sinks = stream, {}

    def write(self, text):
        sink = self.sinks.get(threading.get_ident())
        if sink:
            sink(text)
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


_OUTPUT = _ThreadOutput(sys.stdout)


def _route_output():
    """Put _OUTPUT in front of whatever sys.stdout is now (pytest and others swap it)."""
    if sys.stdout is not _OUTPUT:
        _OUTPUT.stream = sys.stdout
        sys.stdout = _OUTPUT


class Job:
    """One background pipeline job at a time; its printed output is kept as the log."""

    def __init__(self, ledger_path):
        self.ledger_path, self.lock = ledger_path, threading.Lock()
        self.state = {"name": None, "running": False, "log": "", "started": None, "finished": None,
                      "cost": 0.0, "error": None}

    def start(self, name, step):
        """Run step(ledger) in the background. False if a job is already running."""
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"name": name, "running": True, "log": "", "started": time.time(), "finished": None,
                          "cost": 0.0, "error": None}
        threading.Thread(target=self._run, args=(step,), daemon=True).start()
        return True

    def _log(self, text):
        self.state["log"] += text

    def _run(self, step):
        _route_output()
        _OUTPUT.sinks[threading.get_ident()] = self._log
        try:
            with Ledger(self.ledger_path) as ledger:
                step(ledger)
        except Exception as e:  # shown on the page; the server keeps running
            self._log(traceback.format_exc())
            self.state["error"] = str(e)
        finally:
            _OUTPUT.sinks.pop(threading.get_ident(), None)
            self.state.update(running=False, finished=time.time(),
                              cost=llm.total_cost(since=self.state["started"]))


def _structure_svg(smiles, size=260):
    mol = Chem.MolFromSmiles(smiles) if smiles and smiles != "n/a" else None
    if mol is None:
        return None
    drawer = rdMolDraw2D.MolDraw2DSVG(size, size)
    drawer.drawOptions().clearBackground = False
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


def create_app(ledger_path=DATA_DIR / "ledger.db", inbox=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
    inbox = Path(inbox or fetch.INBOX)
    job = Job(ledger_path)
    left = {}  # inbox file -> why the last filing left it there
    just_filed = []  # papers the last filing put files in place for: {doi, title, main, si}
    kinds = {}  # (inbox file, mtime) -> "paper" or "si"

    def file_inbox(ledger):
        filed, skipped = pipeline.do_inbox(ledger, inbox=inbox)
        left.clear()
        left.update(skipped)
        just_filed[:] = [{"doi": doi, "title": (ledger.get(doi) or {}).get("title"),
                          "main": any(p.name == doi_slug(doi) + ".pdf" for p in paths),
                          "si": sum(p.name != doi_slug(doi) + ".pdf" for p in paths)}
                         for doi, paths in filed.items()]
        pipeline.apply_and_export(ledger)

    def kind(path):
        key = (path.name, path.stat().st_mtime)
        if key not in kinds:
            try:
                si = path.suffix.lower() != ".pdf" or fetch._looks_like_si(path, fetch._file_text(path))
            except Exception:  # an unreadable PDF; filing says what's wrong with it
                si = False
            kinds[key] = "si" if si else "paper"
        return kinds[key]

    def remove_unfiled():
        """Delete the inbox files that couldn't be filed (copies of what was dropped)."""
        for path in sorted(p for p in inbox.glob("*") if p.is_file()):
            print(f"removed {path.name}: {left.get(path.name, 'not filed')}")
            path.unlink()
        left.clear()

    def read_papers(batch):
        def step(ledger):
            just_filed.clear()  # they're being read now, so no longer waiting
            file_inbox(ledger)
            just_filed.clear()
            remove_unfiled()  # reading starts: what couldn't be filed by now isn't pending any more
            pipeline.do_extract(ledger, batch)
            pipeline.apply_and_export(ledger)
        return step

    def triage_references(batch):
        def step(ledger):
            pipeline.do_triage(ledger, batch)
            pipeline.apply_and_export(ledger)
        return step

    @app.get("/")
    def page():
        return send_file(PAGE)

    @app.get("/api/state")
    def state():
        with Ledger(ledger_path) as ledger:
            sources = [dict(r) for r in ledger.db.execute("SELECT * FROM sources ORDER BY updated_at DESC")]
            for s in sources:
                s["has_pdf"] = fetch.get_fulltext(s["doi"]) is not None
                s["si"] = [p.name for p in fetch.si_files(s["doi"])]
            by_status = {st: [s for s in sources if s["status"] == st] for st in STATUSES}
            midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            files = sorted((p for p in inbox.glob("*") if p.is_file()), key=lambda p: p.name) if inbox.exists() else []
            return jsonify({
                "data": ledger.records("data"),
                "review": ledger.records("review"),
                "papers": sources,
                "reading": by_status[TO_READ],
                "triage": by_status[TO_TRIAGE],
                "wishlist": [s for st in WISHED for s in by_status[st]],
                "inbox": [{"name": p.name, "kind": kind(p), "why": left.get(p.name, "not filed yet")} for p in files],
                "just_filed": just_filed,
                "job": job.state,
                "spent_today": llm.total_cost(since=midnight),
                "cost_per_paper": COST_PER_PAPER,
                "cost_per_abstract": COST_PER_ABSTRACT,
                "image_tools": ocsr.available(),
            })

    @app.post("/api/upload")
    def upload():
        """Save dropped files in the inbox; with a doi (a wish-list row), name them after it."""
        files = request.files.getlist("files")
        if not files:
            abort(400, "no files")
        doi = clean_doi(request.form["doi"]) if request.form.get("doi") else None
        inbox.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            name = secure_filename(f.filename or "") or "dropped"
            suffix = Path(name).suffix.lower()
            if suffix not in fetch.SI_EXTS:
                return jsonify({"error": f"{f.filename}: only {', '.join(fetch.SI_EXTS)} files are read"}), 400
            if doi:  # first PDF is the main text unless we have it; everything else is SI
                main = suffix == ".pdf" and not fetch.get_fulltext(doi) and doi_slug(doi) + ".pdf" not in saved
                name = doi_slug(doi) + (".pdf" if main else f"_SI_{name}")
            dest = inbox / name
            n = 1
            while dest.exists():
                dest = inbox / f"{Path(name).stem}_{n}{Path(name).suffix}"
                n += 1
            f.save(dest)
            saved.append(dest.name)
        started = job.start("Filing dropped files", file_inbox)
        return jsonify({"saved": saved, "filing": started})

    @app.post("/api/inbox/assign")
    def assign():
        body = request.get_json()
        with Ledger(ledger_path) as ledger:
            try:
                fetch.assign_doi(ledger, body["name"], body["doi"], inbox)
            except (ValueError, FileNotFoundError) as e:
                return jsonify({"error": str(e)}), 400
        return jsonify({"filing": job.start("Filing dropped files", file_inbox)})

    @app.post("/api/inbox/file")
    def file_again():
        started = job.start("Filing dropped files", file_inbox)
        return jsonify({"started": True}) if started else (jsonify({"error": "another job is still running"}), 409)

    @app.post("/api/inbox/remove_all")
    def remove_all():
        if job.state["running"]:  # a running job may be filing them right now
            return jsonify({"error": "wait until the current job is done"}), 409
        removed = sorted(p.name for p in inbox.glob("*") if p.is_file())
        remove_unfiled()
        return jsonify({"removed": removed})

    @app.post("/api/inbox/remove")
    def remove():
        path = inbox / Path(request.get_json()["name"]).name
        if not path.is_file():
            abort(404)
        path.unlink()
        left.pop(path.name, None)
        return jsonify({"removed": path.name})

    @app.post("/api/run")
    def run():
        body = request.get_json()
        batch = max(1, min(int(body.get("batch", 10)), 50))
        if body.get("action") == "read":
            started = job.start(f"Reading up to {batch} paper(s)", read_papers(batch))
        elif body.get("action") == "triage":
            started = job.start(f"Triaging up to {batch} reference(s)", triage_references(batch))
        else:
            abort(400, "unknown action")
        return (jsonify({"started": True}) if started
                else (jsonify({"error": "another job is still running"}), 409))

    @app.post("/api/review")
    def decide():
        body = request.get_json()
        row = {"id": body["id"], "decision": body["decision"], "note": body.get("note") or "",
               **(body.get("edits") or {})}
        with Ledger(ledger_path) as ledger:
            messages = review.apply_rows(ledger, [row])
            try:
                ledger.export()  # keep the CSV tables in step with the page
            except PermissionError as e:
                messages.append(f"could not rewrite {Path(e.filename).name}; close it in Excel")
        return jsonify({"messages": messages})

    @app.get("/api/structure.svg")
    def structure():
        svg = _structure_svg(request.args.get("smiles", ""))
        if svg is None:
            abort(404)
        return app.response_class(svg, mimetype="image/svg+xml")

    @app.get("/pdf/<path:doi>")
    def pdf(doi):
        """The paper's main text, or with ?si=n its n-th SI file."""
        n = request.args.get("si", type=int)
        if n is None:
            text = fetch.get_fulltext(doi)
            return send_file(text["main"]) if text else abort(404)
        si = fetch.si_files(doi)
        return send_file(si[n - 1]) if 1 <= n <= len(si) else abort(404)

    @app.get("/files/<name>")
    def table(name):
        path = OUTPUT / Path(name).name
        if path.suffix != ".csv" or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True)

    return app


def serve(port=8000, open_browser=True):
    if open_browser:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, (f"http://127.0.0.1:{port}",)).start()
    create_app().run(host="127.0.0.1", port=port, threaded=True)
