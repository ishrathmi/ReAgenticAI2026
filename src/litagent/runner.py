"""One complete run: pipeline, optional review agent, output files and the run log.

Used by the command line (__main__.py) and the web app (app/streamlit_app.py), so both run the same code.

  result = run_profile(cfg, schema, profile="peptide_solubility", use_agent=True,
                       sources=None, progress=print_progress)

progress(event, **info) is called at each step (file parsed, passage extracted, repair, agent task and
step). Raise RunCancelled from it to stop a run; the run log then records the run as cancelled.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
from pathlib import Path

from . import agent, output
from .config import ROOT
from .models import CachedModel, OpenRouterModel
from .runlog import RunLogger
from .store import PaperStore


class RunCancelled(BaseException):
    """Raised from a progress callback to stop a run (BaseException so per-passage error handling ignores it)."""


def slug(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", model_id or "").strip("-")


def output_names(cfg: dict, profile: str, model_id: str, agent_model: str | None, use_agent: bool) -> tuple:
    out = cfg.get("output", {})
    base = out.get("basename", profile or "records")
    if model_id and out.get("model_in_filename", True):      # one output file per model, for comparison
        base += "__" + slug(model_id)
    pipeline_name = base + ("__pipeline" if use_agent else "")
    agent_name = f"{base}__agent-{slug(agent_model or 'agent')}" if use_agent else None
    return pipeline_name, agent_name


def run_profile(cfg: dict, schema: dict, profile: str = "", model=None, model_id: str | None = None,
                use_agent: bool = False, root: Path = ROOT, sources: list | None = None, progress=None,
                agent_chat=None, store: PaperStore | None = None, dry_run: bool = False) -> dict:
    """Run the pipeline (and the review agent if use_agent). Returns paths, records and summaries.

    model: an extraction model object (tests); otherwise an OpenRouter model `model_id` with caching,
    a call budget and a run log. sources: explicit source list (see agent.gather_sources); default:
    the profile's local_folders and queries.
    """
    progress = progress or (lambda event, **info: None)
    cfg = copy.deepcopy(cfg)
    acfg = cfg.setdefault("agent", {}) or {}
    use_agent = use_agent and not dry_run
    out_dir = str(root / cfg.get("output", {}).get("dir", "outputs"))
    store = store or PaperStore(agent.store_path(cfg, root))
    log = None
    if model is None and not dry_run:
        mid = model_id or cfg.get("model") or os.getenv("OPENROUTER_MODEL")
        llm = cfg.get("llm", {})
        inner = OpenRouterModel(mid)
        log = RunLogger(str(Path(out_dir) / "runs"), profile, inner.model, root)
        if use_agent:
            log.event("agent_enabled", agent_model=acfg.get("model"))
        model = CachedModel(inner, str(root / llm.get("cache_dir", ".cache/llm")), llm.get("max_calls_per_run", 200),
                            logger=log)
    result = {"log_path": str(log.path) if log else None, "pipeline": None, "agent": None}

    def fail(stage: str, exc: BaseException):
        if log:
            status = "cancelled" if isinstance(exc, RunCancelled) else f"failed{' in ' + stage if stage else ''}: {type(exc).__name__}"
            log.finish(status=status)

    try:
        records, files = agent.run(cfg, schema, model, root=root, dry_run=dry_run, log=log, store=store,
                                   sources=sources, progress=progress)
    except BaseException as exc:          # includes Ctrl-C and cancellation: still save the timing summary
        fail("", exc)
        raise
    result.update(records=records, files=files)
    if dry_run:
        return result

    mid = getattr(model, "model", "")
    pipeline_name, agent_name = output_names(cfg, profile, mid, acfg.get("model"), use_agent)
    label = mid + (" (pipeline)" if use_agent else "")
    run_id = log.run_id if log else ""
    for r in records:
        r["run_label"] = label
        r["run_id"] = run_id                 # lets compare_models.py find this run's cost and time
    csv_path, xlsx_path = output.write(records, files, schema, out_dir, pipeline_name)
    flagged = sum(1 for r in records if r.get("needs_review"))
    result["pipeline"] = {"csv": str(csv_path), "xlsx": str(xlsx_path), "records": len(records), "flagged": flagged}
    progress("pipeline_done", records=len(records), flagged=flagged, csv=str(csv_path))

    extra = {}
    if use_agent:
        from .review_agent import ReviewAgent
        reviewer = ReviewAgent(cfg, schema, store, copy.deepcopy(records), extract_model=model, log=log, root=root,
                               chat=agent_chat, progress=progress)
        try:
            with (log.stage("agent") if log else contextlib.nullcontext()):
                res = reviewer.run()
        except BaseException as exc:
            fail("agent", exc)
            raise
        agent_label = f"{mid} + agent {acfg.get('model')}"
        for r in res["records"] + res["rejected"]:
            r["run_label"] = agent_label
            r["run_id"] = run_id
        tasks = [{"task": t.task_id, "outcome": t.outcome, "file": t.source_file, "location": t.location,
                  "problem": t.reason, "records": len(t.record_ids), "steps": t.steps, "model_calls": t.calls,
                  "seconds": t.seconds, "summary": t.summary, "trace": " -> ".join(t.trace)} for t in res["tasks"]]
        a_csv, a_xlsx = output.write(res["records"], files, schema, out_dir, agent_name,
                                     extra_sheets={"Agent tasks": tasks, "Rejected by agent": res["rejected"]})
        extra = res["summary"]
        result["agent"] = {"csv": str(a_csv), "xlsx": str(a_xlsx), "tasks": tasks, "summary": extra,
                           "records": res["records"], "rejected": res["rejected"]}
        progress("agent_done", **extra)
    if log:
        stopped = any(str(f.get("status", "")).startswith("stopped") for f in files)
        result["run"] = log.finish("stopped: call budget" if stopped else "ok", len(files), len(records), flagged, **extra)
        result["runs_csv"] = str(log.dir / "runs.csv")
    return result
