"""Export a bounded, path-free Kong-Eval evidence report; preserve every trial.

No runtime transcripts, provider response bodies or model credentials are copied.
Review the JSON and run prepare_public_release.py before sharing it.
"""
import argparse
import hashlib
import json
from pathlib import Path


def export(suite: Path, output: Path):
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((suite / "summary.json").read_text(encoding="utf-8"))
    results = []
    for reference in summary["results"]:
        # Derive a bounded location from case/trial metadata, not an absolute saved path.
        case = reference["case"]
        if not case.replace("_", "").isalnum():
            raise ValueError("Invalid case identifier")
        path = suite / "cases" / case / f"trial-{int(reference['repetition']):02d}" / "result.json"
        trial = json.loads(path.read_text(encoding="utf-8"))
        allowed = ("case", "category", "repetition", "mode", "fixture_simulations", "score_scope", "status",
                   "runtime_status", "error_completion", "checks", "model_calls", "turns", "call_records", "usage",
                   "elapsed_seconds", "human_intervention", "protected_hashes", "usage_complete", "failed_tools", "model_errors", "implementation_sha256")
        item = {key:trial[key] for key in allowed if key in trial}
        item["failed_checks"] = [name for name, value in trial["checks"].items() if not value]
        item["raw_result_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Keep error categories, not free-form tool error text or model-generated prose.
        item["failure_category"] = str(trial.get("failure_reason") or "").split(":",1)[0] or None
        item["artifacts"] = []
        for name in ("summary.csv", "process.py", "calc.py", "research.json", "answer.txt", "result.txt", "saved.json", "proof.png"):
            artifact = path.parent / "workspace" / name
            if artifact.is_file() and not artifact.is_symlink() and artifact.resolve().is_relative_to((path.parent / "workspace").resolve()):
                item["artifacts"].append({"path":name,"bytes":artifact.stat().st_size,
                                          "sha256":hashlib.sha256(artifact.read_bytes()).hexdigest()})
        results.append(item)
    public_summary = {key:value for key,value in summary.items() if key != "results"}
    report = {"kind":"kong_eval_public_evidence","schema_version":1,"manifest":manifest,
              "summary":public_summary,"planned_trials":len(manifest["cases"])*manifest["repetitions"],
              "recorded_trials":len(results),"trials":results,
              "scope":"All recorded trials retained. Paths, transcripts and free-form errors omitted. Artifact hashes support identity, not proof of semantic correctness. Rerun the published task contracts and independent verifiers to reproduce the checks."}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x",encoding="utf-8") as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
        stream.write("\n")
    return len(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite",type=Path,help="Kong-Eval UUID suite directory")
    parser.add_argument("output",type=Path,help="New public JSON file; never overwrites")
    args = parser.parse_args()
    count = export(args.suite,args.output)
    print(f"Exported {count} trials: {args.output}")


if __name__ == "__main__":
    main()
