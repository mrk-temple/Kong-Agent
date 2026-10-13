"""Evaluate audited V0.5 evidence; missing studies remain unmeasured. No model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None):
    from kong.evaluation.acceptance import release_assessment
    parser = argparse.ArgumentParser(description="V0.5 release gates, separate from development calibration")
    for flag in ("engineering", "development", "paired", "human", "memory-study", "skill-study", "v04-regression"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--output", type=Path, required=True, help="A new JSON file; prior evidence is never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Use a new output file")
    inputs, identities = {}, {}
    for field in ("engineering", "development", "paired", "human", "memory_study", "skill_study", "v04_regression"):
        path = getattr(args, field)
        if path is None:
            inputs[field] = None
            continue
        with path.open("rb") as stream:
            raw = stream.read(4_000_001)
        if len(raw) > 4_000_000:
            parser.error("Evidence input exceeds its bound")
        inputs[field] = json.loads(raw.decode("utf-8-sig"))
        identities[field] = {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
    assessment = release_assessment(**inputs)
    assessment["evidence_inputs"] = identities
    assessment["scope"] = "Structured audit of caller-verified evidence; not a substitute for external grading or third-party review"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(assessment, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(assessment, ensure_ascii=False, indent=2))
    return 0 if assessment["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
