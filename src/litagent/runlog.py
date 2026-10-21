"""Run logging: timing, token use and cost for every model call and pipeline stage.

Each run writes two things under outputs/runs/:
  <run_id>.jsonl  one line per event (model call, stage, agent step), for detailed analysis
  runs.csv        one summary row per run, appended, for comparing runs over time

The same logger is meant for the future agent loop: call log.event("agent_step", ...)
for each decision the model makes, and wrap tool calls in `with log.stage("tool:<name>")`.
"""

from __future__ import annotations

import csv
import json
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PHASES = ["extract", "repair", "agent"]
SUMMARY_FIELDS = [
    "run_id", "started", "finished", "status", "profile", "model", "agent_model", "git_commit",
    "wall_time_s", "inference_time_s", "inference_share", "other_time_s",
    "llm_calls", "cache_hits", "tokens_in", "tokens_out", "cost_usd",
    "mean_latency_s", "max_latency_s", "files", "records", "flagged",
] + [f"{p}_{k}" for p in PHASES for k in ("calls", "inference_s", "tokens_in", "tokens_out", "cost_usd")] + [
    "agent_tasks", "agent_accepted", "agent_step_limit", "agent_invalid_tool_calls",
    "agent_records_changed", "agent_records_added", "agent_records_rejected",
    "records_after_agent", "flagged_after_agent",
]


def _git_commit(root: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


class RunLogger:
    def __init__(self, out_dir: str, profile: str = "", model: str = "", root: Path | None = None):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.started = datetime.now()
        self.run_id = self.started.strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"{self.run_id}.jsonl"
        self.profile, self.model = profile, model
        self.commit = _git_commit(root or Path.cwd())
        self._t0 = time.perf_counter()
        self.latencies: list = []
        self.cache_hits = 0
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.stage_totals: dict = {}
        self.phase: dict = {}            # phase -> {"calls", "inference_s", "tokens_in", "tokens_out", "cost_usd"}
        self.context: dict = {}          # e.g. {"file": ..., "location": ...}; attached to model-call events
        self.event("run_start", profile=profile, model=model, git_commit=self.commit)

    # -- events ------------------------------------------------------------------
    def event(self, kind: str, **fields) -> None:
        rec = {"t": datetime.now().isoformat(timespec="milliseconds"),
               "elapsed_s": round(time.perf_counter() - self._t0, 3), "event": kind, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def llm_call(self, latency_s: float, usage: dict | None, cache_hit: bool) -> None:
        usage = usage or {}
        if cache_hit:
            self.cache_hits += 1
        else:
            self.latencies.append(latency_s)
            self.tokens_in += usage.get("prompt_tokens") or 0
            self.tokens_out += usage.get("completion_tokens") or 0
            self.cost += usage.get("cost") or 0.0
            ph = self.phase.setdefault(self.context.get("phase", "extract"),
                                       {"calls": 0, "inference_s": 0.0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
            ph["calls"] += 1
            ph["inference_s"] += latency_s
            ph["tokens_in"] += usage.get("prompt_tokens") or 0
            ph["tokens_out"] += usage.get("completion_tokens") or 0
            ph["cost_usd"] += usage.get("cost") or 0.0
        self.event("llm_call", cache_hit=cache_hit, latency_s=round(latency_s, 3), **self.context,
                   tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"),
                   cost_usd=usage.get("cost"))

    @contextmanager
    def stage(self, name: str, **fields):
        """Time a block: with log.stage("parse", file=...): ..."""
        t = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t
            self.stage_totals[name] = self.stage_totals.get(name, 0.0) + dt
            self.event("stage", stage=name, duration_s=round(dt, 3), **fields)

    # -- summary -----------------------------------------------------------------
    def finish(self, status: str = "ok", files: int = 0, records: int = 0, flagged: int = 0, **extra) -> dict:
        wall = time.perf_counter() - self._t0
        inf = sum(self.latencies)
        summary = {
            "run_id": self.run_id, "started": self.started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"), "status": status,
            "profile": self.profile, "model": self.model, "git_commit": self.commit,
            "wall_time_s": round(wall, 2), "inference_time_s": round(inf, 2),
            "inference_share": round(inf / wall, 3) if wall else 0, "other_time_s": round(wall - inf, 2),
            "llm_calls": len(self.latencies), "cache_hits": self.cache_hits,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_usd": round(self.cost, 6),
            "mean_latency_s": round(inf / len(self.latencies), 2) if self.latencies else 0,
            "max_latency_s": round(max(self.latencies), 2) if self.latencies else 0,
            "files": files, "records": records, "flagged": flagged,
        }
        for p in PHASES:
            for k, v in self.phase.get(p, {}).items():
                summary[f"{p}_{k}"] = round(v, 6 if k == "cost_usd" else 2) if isinstance(v, float) else v
        summary.update(extra)
        self.event("run_end", **summary, stage_totals_s={k: round(v, 2) for k, v in self.stage_totals.items()})
        table = self.dir / "runs.csv"
        if table.exists():
            with open(table, encoding="utf-8") as f:
                old = list(csv.DictReader(f))
            if old and list(old[0].keys()) != SUMMARY_FIELDS:      # older header: rewrite with the new columns
                with open(table, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(old)
        new = not table.exists()
        with open(table, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(summary)
        return summary
