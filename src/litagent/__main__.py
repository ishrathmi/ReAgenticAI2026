"""Command-line entry point.

  python run_agent.py --profile peptide_solubility --dry-run   # preview; no API calls
  python run_agent.py --profile peptide_solubility             # extract and write the database
  python run_agent.py --profile peptide_solubility --agent     # then run the review agent
"""

from __future__ import annotations

import argparse
import sys

from .config import load_profile
from .runner import run_profile


def main(argv=None, model=None) -> int:
    ap = argparse.ArgumentParser(prog="litagent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="folder under config/ (e.g. peptide_solubility)")
    ap.add_argument("--model", help="OpenRouter model ID (overrides profile and .env)")
    ap.add_argument("--dry-run", action="store_true", help="parse and prefilter only; no API calls")
    ap.add_argument("--agent", dest="agent", action="store_true", default=None,
                    help="run the review agent after the pipeline (default: agent.enabled in the profile)")
    ap.add_argument("--no-agent", dest="agent", action="store_false", help="pipeline only")
    ap.add_argument("--agent-model", help="OpenRouter model ID for the review agent (overrides agent.model)")
    args = ap.parse_args(argv)

    cfg, schema = load_profile(args.profile)
    acfg = cfg.setdefault("agent", {}) or {}
    if args.agent_model:
        acfg["model"] = args.agent_model
    use_agent = acfg.get("enabled", False) if args.agent is None else args.agent

    r = run_profile(cfg, schema, profile=args.profile or "", model=model, model_id=args.model,
                    use_agent=use_agent, dry_run=args.dry_run)
    if args.dry_run:
        print("\nDry run complete; no API calls made.")
        return 0
    p = r["pipeline"]
    print(f"\n{p['records']} records ({p['flagged']} flagged for review)\nCSV:   {p['csv']}\nExcel: {p['xlsx']}")
    if r.get("agent"):
        a, s = r["agent"], r["agent"]["summary"]
        print(f"\nAfter review agent: {s['records_after_agent']} records ({s['flagged_after_agent']} flagged); "
              f"{s['agent_records_changed']} changed, {s['agent_records_added']} added, "
              f"{s['agent_records_rejected']} rejected; {s['agent_accepted']}/{s['agent_tasks']} tasks finished")
        print(f"CSV:   {a['csv']}\nExcel: {a['xlsx']}")
    s = r.get("run")
    if s:
        print(f"Wall time {s['wall_time_s']} s; inference {s['inference_time_s']} s "
              f"({s['inference_share']:.0%}) over {s['llm_calls']} calls "
              f"(mean {s['mean_latency_s']} s, max {s['max_latency_s']} s; {s['cache_hits']} cached)")
        print(f"Tokens in/out: {s['tokens_in']}/{s['tokens_out']}; cost: ${s['cost_usd']}")
        for ph in ("extract", "repair", "agent"):
            if s.get(f"{ph}_calls"):
                print(f"  {ph:<8} {s[f'{ph}_calls']} calls, {s[f'{ph}_inference_s']} s, ${s.get(f'{ph}_cost_usd') or 0}")
        print(f"Run log: {r['log_path']}\nAll runs: {r['runs_csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
