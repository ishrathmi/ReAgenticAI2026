"""Command-line entry point.

  python run_agent.py --profile peptide_solubility --dry-run   # preview; no API calls
  python run_agent.py --profile peptide_solubility             # extract and write the database
"""

from __future__ import annotations

import argparse
import re
import os
import sys

from . import agent, output
from .config import ROOT, load_profile
from .models import CachedModel, OpenRouterModel
from .runlog import RunLogger


def main(argv=None, model=None) -> int:
    ap = argparse.ArgumentParser(prog="litagent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="folder under config/ (e.g. peptide_solubility)")
    ap.add_argument("--model", help="OpenRouter model ID (overrides profile and .env)")
    ap.add_argument("--dry-run", action="store_true", help="parse and prefilter only; no API calls")
    args = ap.parse_args(argv)

    cfg, schema = load_profile(args.profile)
    out = cfg.get("output", {})
    log = None
    if model is None and not args.dry_run:
        model_id = args.model or cfg.get("model") or os.getenv("OPENROUTER_MODEL")
        llm = cfg.get("llm", {})
        inner = OpenRouterModel(model_id)
        log = RunLogger(str(ROOT / out.get("dir", "outputs") / "runs"), args.profile or "", inner.model, ROOT)
        model = CachedModel(inner, str(ROOT / llm.get("cache_dir", ".cache/llm")),
                            llm.get("max_calls_per_run", 200), logger=log)

    try:
        records, files = agent.run(cfg, schema, model, root=ROOT, dry_run=args.dry_run, log=log)
    except BaseException as exc:          # includes Ctrl-C: still save the timing summary
        if log:
            log.finish(status=f"failed: {type(exc).__name__}")
        raise
    if args.dry_run:
        print("\nDry run complete; no API calls made.")
        return 0

    basename = out.get("basename", args.profile or "records")
    model_id = getattr(model, "model", "")
    if model_id and out.get("model_in_filename", True):      # keep one output file per model for comparison
        basename += "__" + re.sub(r"[^A-Za-z0-9.]+", "-", model_id).strip("-")
    csv_path, xlsx_path = output.write(records, files, schema, str(ROOT / out.get("dir", "outputs")), basename)
    flagged = sum(1 for r in records if r.get("needs_review"))
    print(f"\n{len(records)} records ({flagged} flagged for review)")
    print(f"CSV:   {csv_path}\nExcel: {xlsx_path}")
    if log:
        stopped = any(str(f.get("status", "")).startswith("stopped") for f in files)
        s = log.finish("stopped: call budget" if stopped else "ok", len(files), len(records), flagged)
        print(f"Wall time {s['wall_time_s']} s; inference {s['inference_time_s']} s "
              f"({s['inference_share']:.0%}) over {s['llm_calls']} calls "
              f"(mean {s['mean_latency_s']} s, max {s['max_latency_s']} s; {s['cache_hits']} cached)")
        print(f"Tokens in/out: {s['tokens_in']}/{s['tokens_out']}; cost: ${s['cost_usd']}")
        print(f"Run log: {log.path}\nAll runs: {log.dir / 'runs.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
