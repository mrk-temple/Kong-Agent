"""Kong-Eval CLI. Default runs deterministic offline fixtures, never a paid API."""
import argparse
import asyncio
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kong.config import load_config
from kong.evaluation import CASES, run_suite


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run independently checked Kong-Eval trials")
    parser.add_argument("--list", action="store_true", help="List the 12 task IDs and scoring scope")
    parser.add_argument("--cases", nargs="+", choices=[c.id for c in CASES], help="Select task IDs (default: all)")
    parser.add_argument("--repetitions", type=int, default=3, help="Independent trials per task (default: 3)")
    parser.add_argument("--output", type=Path, default=ROOT / ".kong" / "kong-eval", help="Parent directory for a new UUID suite")
    parser.add_argument("--live", action="store_true", help="Explicitly enable real model requests; may incur charges")
    parser.add_argument("--config", type=Path, help="TOML model configuration for --live")
    parser.add_argument("--profile", help="Model profile name for --live; API key comes from its api_key_env")
    parser.add_argument("--allow-no-key", action="store_true", help="Permit an explicit no-key profile only for a loopback model server")
    args = parser.parse_args(argv)
    if args.list:
        for case in CASES:
            print(f"{case.id}\t{case.category}\t{case.score_scope}")
        return 0
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    config = None
    if args.live:
        if not args.config or not args.profile:
            parser.error("--live requires --config and --profile")
        try:
            config = load_config(args.config, args.profile)
        except (ValueError, OSError) as exc:
            parser.error(f"Model configuration failed: {exc}")
        if not config.api_key.get_secret_value() and not (args.allow_no_key and urlsplit(config.base_url).hostname in {"localhost", "127.0.0.1", "::1"}):
            parser.error("The selected profile's api_key_env is unset; use --allow-no-key only for a loopback model server; no request was made")
    elif args.config or args.profile or args.allow_no_key:
        parser.error("--config, --profile and --allow-no-key require --live")
    root, summary = asyncio.run(run_suite(args.output, case_ids=args.cases, repetitions=args.repetitions,
                                          live=args.live, config=config, profile=args.profile))
    print(f"Report: {root / 'summary.json'}")
    return 0 if summary["counts"]["failed"] == summary["counts"]["environment_error"] == summary["counts"]["timeout"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
