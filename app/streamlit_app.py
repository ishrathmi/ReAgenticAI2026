"""Literature agent: web app (local demo).

  streamlit run app/streamlit_app.py

Run tab:    choose papers (local folder, uploads, Europe PMC search), set models, start a run, watch progress
            and every agent step live.
Review tab: browse an output database, filter flagged records, open a record to see its source passage and
            agent trace, edit / accept / reject it, and download the reviewed CSV or Excel file.

Uses the OpenRouter key in .env. Runs execute in a background thread, so the Review tab stays usable during
a run. Uploaded files are kept outside the repository (~/.cache/litagent/uploads) and are never committed.
"""

from __future__ import annotations

import html
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from litagent import agent as pipeline  # noqa: E402
from litagent import output  # noqa: E402
from litagent.config import CONFIG_DIR, load_profile  # noqa: E402
from litagent.runner import RunCancelled, run_profile  # noqa: E402
from litagent.store import PaperStore  # noqa: E402
from litagent.tools.parse_pdf import SUPPORTED  # noqa: E402

load_dotenv(ROOT / ".env")
UPLOADS = Path.home() / ".cache" / "litagent" / "uploads"
MODEL_CHOICES = ["openai/gpt-4o-mini", "openai/gpt-6.1-sol", "anthropic/claude-sonnet-5.5",
                 "anthropic/claude-haiku-5.5", "google/gemini-4-argon", "anthropic/claude-opus-5.5"]
DETAIL_FIELDS = ["peptide_name", "sequence", "n_terminus", "c_terminus", "modifications", "salt_form",
                 "solubility_operator", "solubility_value", "solubility_unit", "solubility_qualitative",
                 "method", "solvent", "temperature_c", "ph", "buffer", "notes"]
TABLE_COLUMNS = ["record_id", "user_status", "needs_review", "peptide_name", "sequence", "solubility_operator",
                 "solubility_value", "solubility_unit", "solubility_qualitative", "solvent", "temperature_c", "ph",
                 "review_reason", "agent_status", "source_file", "location"]

st.set_page_config(page_title="Peptide solubility agent", layout="wide")
# Palette accents the theme file cannot set: Copperwood headings, Sunflower Gold highlight for quotes
st.markdown("""<style>
h1, h2, h3 { color: #b1740f !important; }
mark { background: #fdb833; color: #1b065e; padding: 0 2px; border-radius: 3px; }
</style>""", unsafe_allow_html=True)


# --- background runs ---------------------------------------------------------------------------

class Job:
    """A run in a background thread. The progress callback records state the page reads every 2 s."""

    def __init__(self):
        self.lines: list = []
        self.steps: list = []            # agent steps
        self.stage = "starting"
        self.fraction = 0.0
        self.status = "running"          # running / done / failed / cancelled
        self.result = None
        self.error = ""
        self.cancel = False
        self.started = time.time()

    def progress(self, event: str, **i):
        if self.cancel:
            raise RunCancelled()
        if event == "sources":
            self.log(f"{i['n']} file(s) to process")
        elif event == "parse":
            self.stage = f"Reading {i['file']}"
        elif event == "extract":
            done = (i["file_no"] - 1 + i["chunk_no"] / max(i["chunks"], 1)) / max(i["files"], 1)
            self.fraction = 0.6 * done
            self.stage = f"Extracting: file {i['file_no']}/{i['files']} ({i['file']}), passage {i['chunk_no']}/{i['chunks']}"
            if i["chunk_no"] == 1:
                self.log(f"Extracting {i['file']} ({i['chunks']} passages)")
        elif event == "repair":
            self.fraction = 0.6 + 0.1 * i["record_no"] / max(i["records"], 1)
            self.stage = f"Repair: record {i['record_no']}/{i['records']}"
        elif event == "pipeline_done":
            self.fraction = 0.7
            self.log(f"Pipeline done: {i['records']} records, {i['flagged']} flagged")
        elif event == "agent_tasks":
            self.log(f"Review agent ({i['model']}): {i['n']} task(s)")
        elif event == "agent_task_start":
            self.fraction = 0.7 + 0.3 * (i["task_no"] - 1) / max(i["tasks"], 1)
            self.stage = f"Agent: task {i['task_no']}/{i['tasks']} - {i['reason'][:70]}"
        elif event == "agent_step":
            args = json.dumps(i["args"], ensure_ascii=False)
            self.steps.append(f"{i['task']} step {i['step']}: {i['tool']}({args[:160]}) -> {i['result'][:160]}")
        elif event == "agent_task_end":
            self.log(f"{i['task']} [{i['outcome']}] {i['steps']} steps: {i['summary'][:120]}")
        elif event == "agent_done":
            self.fraction = 1.0
            self.log(f"Agent done: {i['agent_records_changed']} changed, {i['agent_records_added']} added, "
                     f"{i['agent_records_rejected']} rejected")

    def log(self, line: str):
        self.lines.append(f"{datetime.now():%H:%M:%S}  {line}")


def start_job(cfg, schema, profile, model_id, use_agent, sources) -> Job:
    job = Job()

    def work():
        try:
            job.result = run_profile(cfg, schema, profile=profile, model_id=model_id, use_agent=use_agent,
                                     sources=sources, progress=job.progress)
            job.status, job.stage, job.fraction = "done", "Finished", 1.0
        except RunCancelled:
            job.status, job.stage = "cancelled", "Cancelled"
        except BaseException as exc:            # shown on the page
            job.status, job.stage = "failed", "Failed"
            job.error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-2000:]}"

    threading.Thread(target=work, daemon=True).start()
    return job


# --- helpers ---------------------------------------------------------------------------------------

def profiles() -> list:
    return sorted(p.name for p in CONFIG_DIR.iterdir() if (p / "papers.yaml").exists() and (p / "schema.json").exists())


def session_dir() -> Path:
    sid = st.session_state.setdefault("session_id", uuid.uuid4().hex[:10])
    d = UPLOADS / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_files(cfg) -> pd.DataFrame:
    rows = []
    for folder in cfg.get("local_folders") or []:
        base = ROOT / folder if not Path(folder).is_absolute() else Path(folder)
        for p in sorted(base.rglob("*")):
            if p.suffix.lower() in SUPPORTED and not p.name.startswith(("~$", ".")):
                rows.append({"include": True, "file": p.name, "paper": pipeline.group_of(p.name, cfg.get("paper_groups") or {}),
                             "folder": str(p.parent.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p.parent),
                             "path": str(p)})
    return pd.DataFrame(rows, columns=["include", "file", "paper", "folder", "path"])


def output_files(cfg) -> list:
    out = ROOT / cfg.get("output", {}).get("dir", "outputs")
    return sorted(out.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True) if out.exists() else []


def none_if_nan(v):
    return None if (v is None or (isinstance(v, float) and pd.isna(v)) or v == "") else v


# --- page ------------------------------------------------------------------------------------------

st.title("Peptide solubility agent")
key_ok = bool(os.getenv("OPENROUTER_API_KEY"))

with st.sidebar:
    st.header("Settings")
    names = profiles()
    profile = st.selectbox("Profile", names, index=names.index("peptide_solubility") if "peptide_solubility" in names else 0)
    cfg, schema = load_profile(profile)
    acfg = cfg.setdefault("agent", {})
    default_model = cfg.get("model") or MODEL_CHOICES[0]
    choices = list(dict.fromkeys([default_model] + MODEL_CHOICES))
    model_id = st.selectbox("Extraction model", choices, index=0, accept_new_options=True)
    use_agent = st.toggle("Run review agent", value=bool(acfg.get("enabled", True)))
    agent_choices = list(dict.fromkeys([acfg.get("model", "openai/gpt-6.1-sol")] + MODEL_CHOICES))
    agent_model = st.selectbox("Agent model", agent_choices, index=0, disabled=not use_agent, accept_new_options=True)
    with st.expander("Limits"):
        cfg.setdefault("llm", {})["max_calls_per_run"] = st.number_input(
            "Extraction + repair calls (max)", 10, 2000, int(cfg.get("llm", {}).get("max_calls_per_run", 200)), 10)
        acfg["max_calls"] = st.number_input("Agent calls (max)", 5, 1000, int(acfg.get("max_calls", 120)), 5)
        acfg["max_steps_per_task"] = st.number_input("Agent steps per task", 2, 20, int(acfg.get("max_steps_per_task", 6)))
        cfg.setdefault("repair", {})["enabled"] = st.checkbox("Repair step", value=bool((cfg.get("repair") or {}).get("enabled", True)))
    acfg["model"] = agent_model
    st.divider()
    st.caption(("OpenRouter key found in .env" if key_ok else "No OPENROUTER_API_KEY in .env: runs are disabled"))
    st.caption(f"Paper store: {pipeline.store_path(cfg, ROOT)}")

tab_run, tab_review = st.tabs(["Run", "Review"])

# --- Run tab ---------------------------------------------------------------------------------------
with tab_run:
    job: Job | None = st.session_state.get("job")
    running = bool(job and job.status == "running")

    st.subheader("1. Papers")
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("**From the project folder**")
        files_df = local_files(cfg)
        if files_df.empty:
            st.info("No files in the profile's local_folders.")
            edited = files_df
        else:
            edited = st.data_editor(files_df, hide_index=True, width="stretch", disabled=["file", "paper", "folder"],
                                    column_config={"path": None, "include": st.column_config.CheckboxColumn("use")},
                                    key=f"local_{profile}")
            st.caption("Files in the same 'paper' are read together (main text + SI). Groups come from paper_groups in papers.yaml.")
    with c2:
        st.markdown("**Upload files**")
        ups = st.file_uploader("PDF, XLSX or DOCX (main text and SI)", type=["pdf", "xlsx", "docx"],
                               accept_multiple_files=True)
        same_paper = st.checkbox("Uploaded files belong to one paper", value=True)
        up_name = st.text_input("Paper name for the uploads", value="uploaded_paper", disabled=not same_paper)
        st.caption("Uploads stay on this computer (outside the repository) and are not shared.")

    with st.expander("Search Europe PMC (open-access papers, downloaded with their SI)"):
        q1, q2, q3 = st.columns([4, 1, 1])
        query = q1.text_input("Search", placeholder="peptide solubility DMSO", label_visibility="collapsed")
        limit = q2.number_input("Results", 1, 50, 10, label_visibility="collapsed")
        if q3.button("Search", width="stretch") and query:
            from litagent.tools.europepmc import search as epmc_search
            try:
                hits = epmc_search(query, int(limit))
                st.session_state["epmc"] = pd.DataFrame(
                    [{"include": False, "title": h["title"], "year": h["published"], "authors": (h["authors"] or "")[:60],
                      "has SI": h["has_si"], "licence": h["license"], "pmcid": h["pmcid"], "doi": h["doi"]} for h in hits])
                st.session_state["epmc_items"] = {h["pmcid"]: h for h in hits}
            except Exception as exc:
                st.error(f"Europe PMC search failed: {exc}")
        epmc_df = st.session_state.get("epmc")
        epmc_sel = pd.DataFrame()
        if epmc_df is not None and not epmc_df.empty:
            epmc_sel = st.data_editor(epmc_df, hide_index=True, width="stretch",
                                      disabled=[c for c in epmc_df.columns if c != "include"], key="epmc_editor")
        elif epmc_df is not None:
            st.info("No open-access results with full text.")

    st.subheader("2. Run")
    n_local = int(edited["include"].sum()) if not edited.empty else 0
    n_up = len(ups or [])
    n_epmc = int(epmc_sel["include"].sum()) if not epmc_sel.empty else 0
    st.write(f"{n_local} project file(s), {n_up} upload(s), {n_epmc} Europe PMC paper(s) selected. "
             f"Extraction: `{model_id}`" + (f", agent: `{agent_model}`" if use_agent else ", no agent") + ".")
    b1, b2, _ = st.columns([1, 1, 4])
    start = b1.button("Run", type="primary", disabled=running or not key_ok or not (n_local + n_up + n_epmc))
    if b2.button("Stop", disabled=not running):
        job.cancel = True
        st.toast("Stopping after the current model call...")

    if start:
        sources = []
        groups = cfg.get("paper_groups") or {}
        for _, row in edited[edited["include"]].iterrows():
            sources.append({"path": Path(row["path"]), "meta": {"source": "local file"},
                            "paper_key": pipeline.group_of(row["file"], groups), "role": None})
        if ups:
            d = session_dir()
            for f in ups:
                p = d / f.name
                p.write_bytes(f.getbuffer())
                sources.append({"path": p, "meta": {"source": "upload"},
                                "paper_key": f"upload:{up_name}" if same_paper else f"upload:{f.name}", "role": None})
        if n_epmc:
            download = str(ROOT / cfg.get("download_dir", ".cache/papers"))
            with st.spinner("Downloading from Europe PMC..."):
                for _, row in epmc_sel[epmc_sel["include"]].iterrows():
                    sources += pipeline.europepmc_sources(st.session_state["epmc_items"][row["pmcid"]], download)
        st.session_state["job"] = start_job(cfg, schema, profile, model_id, use_agent, sources)
        st.rerun()

    @st.fragment(run_every=2)
    def show_job():
        job: Job | None = st.session_state.get("job")
        if not job:
            st.caption("No run started in this session.")
            return
        elapsed = int(time.time() - job.started)
        st.progress(min(job.fraction, 1.0), text=f"{job.stage}  ({elapsed // 60} min {elapsed % 60:02d} s)")
        if job.status == "failed":
            st.error(job.error)
        elif job.status == "cancelled":
            st.warning("Run cancelled. Outputs written before the stop are kept.")
        elif job.status == "done":
            r = job.result
            p, a = r["pipeline"], r.get("agent")
            m = st.columns(5)
            m[0].metric("Records", (a or p)["records"] if not a else a["summary"]["records_after_agent"])
            m[1].metric("Flagged after pipeline", p["flagged"])
            if a:
                m[2].metric("Flagged after agent", a["summary"]["flagged_after_agent"])
                m[3].metric("Changed by agent", a["summary"]["agent_records_changed"])
            if r.get("run"):
                m[4].metric("Cost", f"${float(r['run']['cost_usd']):.2f}", f"{float(r['run']['wall_time_s']) / 60:.1f} min",
                            delta_color="off")
            st.success(f"Results: {Path((a or p)['csv']).name}. Open the Review tab to check them.")
            st.session_state["review_default"] = (a or p)["csv"]
        cols = st.columns(2)
        with cols[0]:
            st.markdown("**Log**")
            st.code("\n".join(job.lines[-40:]) or "...", language=None)
        with cols[1]:
            st.markdown(f"**Agent steps** ({len(job.steps)})")
            st.code("\n".join(job.steps[-25:]) or "(none yet)", language=None)
        if job.status != "running":
            st.session_state["job_finished"] = True

    st.subheader("3. Progress")
    show_job()

# --- Review tab ------------------------------------------------------------------------------------
with tab_review:
    files = output_files(cfg)
    if not files:
        st.info("No output files yet. Start a run on the Run tab.")
        st.stop()
    names = [f.name for f in files]
    default = Path(st.session_state.get("review_default", files[0])).name
    pick = st.selectbox("Database", names, index=names.index(default) if default in names else 0,
                        help="Newest first. '__pipeline' = before the agent, '__agent-...' = after the agent.")
    path = files[names.index(pick)]
    decisions_path = path.with_suffix(".review.json")
    decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}

    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"record_id": str})
    df["user_status"] = ""
    for rid, d in decisions.items():
        mask = df["record_id"] == rid
        for k, v in (d.get("edits") or {}).items():
            if k in df.columns:
                df.loc[mask, k] = v
        df.loc[mask, "user_status"] = d.get("status", "")

    f1, f2, f3, f4 = st.columns([1, 2, 2, 2])
    only_flagged = f1.checkbox("Flagged only", value=True)
    file_sel = f2.multiselect("File", sorted(df["source_file"].dropna().unique()))
    status_sel = f3.multiselect("Your decision", ["", "accepted", "edited", "rejected"], format_func=lambda s: s or "not reviewed")
    text = f4.text_input("Search peptide / solvent / reason")
    view = df
    if only_flagged:
        view = view[view["needs_review"].astype(str).str.lower() == "true"]
    if file_sel:
        view = view[view["source_file"].isin(file_sel)]
    if status_sel:
        view = view[view["user_status"].isin(status_sel)]
    if text:
        blob = view[["peptide_name", "solvent", "review_reason"]].astype(str).agg(" ".join, axis=1).str.lower()
        view = view[blob.str.contains(text.lower(), regex=False)]
    total, flagged = len(df), int((df["needs_review"].astype(str).str.lower() == "true").sum())
    st.caption(f"{len(view)} shown · {total} records · {flagged} flagged · {len(decisions)} reviewed by you")

    cols = [c for c in TABLE_COLUMNS if c in view.columns]
    sel = st.dataframe(view[cols], hide_index=True, width="stretch", height=360,
                       selection_mode="single-row", on_select="rerun", key=f"table_{pick}")
    rows = sel.selection.rows if sel and sel.selection else []

    if rows:
        rec = view.iloc[rows[0]].to_dict()
        rid = str(rec["record_id"])
        st.divider()
        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"#### Record {rid}: {rec.get('peptide_name') or '(no name)'}")
            if str(rec.get("needs_review")).lower() == "true":
                st.warning(f"Flagged: {rec.get('review_reason')}")
            if none_if_nan(rec.get("data_caveats")):
                st.caption(f"Source caveats: {rec.get('data_caveats')}")
            quote = none_if_nan(rec.get("evidence")) or ""
            st.markdown(f"**Evidence quote** ({'found in source' if str(rec.get('evidence_verified')).lower() == 'true' else 'NOT found in source'})")
            st.markdown(f"> {html.escape(str(quote))}")
            chunk = None
            if none_if_nan(rec.get("chunk_id")) is not None:
                try:
                    chunk = PaperStore(pipeline.store_path(cfg, ROOT)).chunk(int(float(rec["chunk_id"])))
                except Exception:
                    chunk = None
            st.markdown(f"**Source passage** · {rec.get('source_file')} · {rec.get('location')}"
                        + (f" · section: {chunk.section}" if chunk else ""))
            if chunk:
                body = html.escape(chunk.text)
                q = html.escape(str(quote)).strip()
                if q and q in body:
                    body = body.replace(q, f"<mark>{q}</mark>", 1)
                st.markdown(f"<div style='white-space:pre-wrap;font-size:0.85rem;max-height:320px;overflow:auto;"
                            f"border:1px solid rgba(128,128,128,.3);border-radius:6px;padding:8px'>{body}</div>",
                            unsafe_allow_html=True)
            else:
                st.caption("Passage not available in the paper store.")
            for label, key in (("Agent changes", "agent_changes"), ("Agent trace", "agent_trace"),
                               ("Repair", "repaired_fields"), ("Repair note", "repair_note")):
                if none_if_nan(rec.get(key)):
                    st.markdown(f"**{label}:** {rec.get(key)}")
        with right:
            with st.form(f"edit_{pick}_{rid}"):
                st.markdown("**Fields**")
                vals = {f: st.text_input(f, value="" if none_if_nan(rec.get(f)) is None else str(rec.get(f)))
                        for f in DETAIL_FIELDS if f in rec}
                note = st.text_input("Your note", value=(decisions.get(rid) or {}).get("note", ""))
                a, b, c = st.columns(3)
                save = a.form_submit_button("Save edits", type="primary")
                accept = b.form_submit_button("Accept")
                reject = c.form_submit_button("Reject")
            if save or accept or reject:
                edits = {f: v for f, v in vals.items() if (none_if_nan(rec.get(f)) is None and v) or
                         (none_if_nan(rec.get(f)) is not None and v != str(rec.get(f)))}
                status = "rejected" if reject else ("edited" if (save and edits) else "accepted")
                decisions[rid] = {"status": status, "edits": edits, "note": note, "at": datetime.now().isoformat(timespec="seconds")}
                decisions_path.write_text(json.dumps(decisions, indent=1, ensure_ascii=False))
                st.toast(f"Record {rid}: {status}")
                st.rerun()

    st.divider()
    kept = df[df["user_status"] != "rejected"].copy()
    kept["needs_review"] = kept.apply(lambda r: False if r["user_status"] in ("accepted", "edited") else r["needs_review"], axis=1)
    records = [{k: none_if_nan(v) for k, v in r.items()} for r in kept.drop(columns=["record_id"]).to_dict("records")]
    d1, d2 = st.columns(2)
    d1.download_button("Download reviewed CSV", kept.to_csv(index=False).encode("utf-8-sig"),
                       file_name=path.stem + "__reviewed.csv", mime="text/csv")
    with tempfile.TemporaryDirectory() as tmp:
        _, xlsx = output.write(records, [], schema, tmp, path.stem + "__reviewed")
        d2.download_button("Download reviewed Excel", Path(xlsx).read_bytes(), file_name=Path(xlsx).name,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption("Downloads leave out records you rejected; records you accepted or edited are no longer flagged. "
               f"Your decisions are saved next to the database in {decisions_path.name}.")
