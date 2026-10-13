"""Synthetic Phase 3.5 validation. Requires real config unless --prepare-only."""
import argparse
import asyncio
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from kong.evaluation.living_memory import LivingBudget, run_living_memory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true", help="Engineering packet checks only; no generated or simulated answers")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--compare-profile", help="Four matched-packet probes with a second real model")
    parser.add_argument("--allow-remote", action="store_true", help="Explicit authorization for potentially paid synthetic-data requests")
    parser.add_argument("--max-requests", type=int, default=32)
    parser.add_argument("--max-seconds", type=float, default=900)
    parser.add_argument("--max-estimated-tokens", type=int, default=900_000)
    args = parser.parse_args()
    primary = comparison = None
    if args.prepare_only:
        if args.config or args.profile or args.compare_profile or args.allow_remote:
            parser.error("Prepare-only cannot select models or authorize remote requests")
    else:
        if args.config is None:
            parser.error("Select a real --config; use --prepare-only for engineering checks")
        from kong.config import load_config
        from kong.evaluation.living_memory import TracedCompatibleModel
        primary = TracedCompatibleModel(load_config(args.config, args.profile))
        if args.compare_profile:
            comparison = TracedCompatibleModel(load_config(args.config, args.compare_profile))
    report, path = asyncio.run(run_living_memory(args.output, primary, comparison_model=comparison,
        allow_remote=args.allow_remote, prepare_only=args.prepare_only,
        budget=LivingBudget(max_requests=args.max_requests, max_seconds=args.max_seconds, max_estimated_tokens=args.max_estimated_tokens)))
    print(f"{report['behavior_source']}: {report['answered']} answers; stop={report['stop_reason']}; report={path}")
    return 1 if report["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
