"""V0.5 development calibration CLI. Offline by default; explicit live opt-in."""
import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kong.config import load_config
from kong.evaluation.long_driver import freeze_resources, run_long_suite
from kong.evaluation.live_manifest import LiveManifest
from kong.evaluation.scenarios import DEVELOPMENT_LIBRARY, DEVELOPMENT_SCENARIOS


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run two V0.5 long-task development calibrations")
    parser.add_argument("--image", required=True, help="An existing local Python Docker image; no pull or host fallback")
    parser.add_argument("--output", type=Path, default=ROOT / ".kong" / "long-eval")
    parser.add_argument("--live", action="store_true", help="Explicit opt-in to paid model requests")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--all-development", action="store_true", help="Run all eight seen development tasks; default remains two calibrations")
    parser.add_argument("--manifest", type=Path, help="Previously frozen source/task/model/budget manifest for an expanded live batch")
    parser.add_argument("--freeze-manifest", type=Path, help="Create a development manifest without making model requests")
    args = parser.parse_args(argv)
    config = None
    if args.live or args.freeze_manifest:
        if not args.config or not args.profile:
            parser.error("--live requires explicit --config and --profile")
        config = load_config(args.config, args.profile)
        if args.live and not config.api_key.get_secret_value():
            parser.error("Configured credential is unavailable; no request made")
    elif args.config or args.profile:
        parser.error("Config/profile require --live")
    scenarios = DEVELOPMENT_LIBRARY if args.all_development else DEVELOPMENT_SCENARIOS
    if args.freeze_manifest:
        if args.live or args.manifest or args.freeze_manifest.exists():
            parser.error("Freeze into a new file, without --live or --manifest")
        import subprocess
        candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--",
                                    "src", "tests", "examples", "pyproject.toml", "uv.lock"], cwd=ROOT):
            parser.error("Commit the selected source, tests, entry points and lockfile before freezing")
        manifest = LiveManifest(candidate_commit=candidate, resources=freeze_resources(), image=args.image,
            model=config.model_dump(mode="json"), scenarios=[s.manifest() for s in scenarios],
            max_requests=sum(s.budget.requests for s in scenarios),
            max_total_tokens=sum(s.budget.total_tokens for s in scenarios),
            max_active_seconds=sum(s.budget.active_seconds for s in scenarios))
        args.freeze_manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.freeze_manifest.open("x", encoding="utf-8") as stream:
            stream.write(manifest.model_dump_json(indent=2))
        print(f"Frozen development manifest: {args.freeze_manifest}; no model requests made")
        return 0
    if args.manifest:
        if not args.live:
            parser.error("--manifest requires explicit --live")
        manifest = LiveManifest.model_validate_json(args.manifest.read_text(encoding="utf-8-sig"))
        manifest.verify(scenarios, freeze_resources(), args.image, config.model_dump(mode="json"))
    if args.live and args.all_development and not args.manifest:
        parser.error("The eight-task library requires its own frozen live manifest; this CLI only enables the two-task live calibration")
    root, summary = asyncio.run(run_long_suite(args.output, args.image, config=config,
        scenarios=scenarios))
    print(f"Report: {root / 'summary.json'}")
    return 0 if summary["complete"] and all(r["status"] == "success" for r in summary["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
