"""Run only synthetic Memory fixtures. Offline by default; no existing DB input."""
import argparse
import asyncio
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from kong.evaluation.memory_behavior import EvalBudget, run_memory_behavior
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--allow-remote", action="store_true", help="Explicit authorization to send SYNTHETIC fixtures to a potentially paid provider")
    parser.add_argument("--max-requests", type=int, default=24)
    parser.add_argument("--max-seconds", type=float, default=180)
    args = parser.parse_args()
    model, name = None, "offline-test-double"
    if args.live:
        if args.config is None:
            parser.error("--live requires an explicit --config")
        from kong.config import load_config
        from kong.models.compatible import CompatibleModel
        model = CompatibleModel(load_config(args.config, args.profile))
        name = model.config.model
    elif args.allow_remote or args.config or args.profile:
        parser.error("Model configuration and remote authorization require --live")
    report, path = asyncio.run(run_memory_behavior(args.output, model, model_name=name, live=args.live,
        allow_remote=args.allow_remote, budget=EvalBudget(max_requests=args.max_requests, max_seconds=args.max_seconds)))
    print(f"{report['behavior_source']}: {report['answered']}/{report['planned_requests']} answered; report: {path}")
    return 1 if report["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
