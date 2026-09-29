"""One regression entry point. Default: deterministic fixtures, no paid APIs.

--live explicitly adds six real-model scenarios; every run and failure is retained.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "data":["tests/test_local_execution.py::test_local_read_execute_artifact_verify_runtime"],
    "research":["tests/test_web_research.py::test_search_provider_mapping_and_failed_response_not_cached",
                "tests/test_web_research.py::test_fetch_snapshot_pagination_cache_and_progress"],
    "repair":["tests/test_release.py::test_repair_runtime_requires_real_check_after_patch"],
    "terminal":["tests/test_integrations.py::test_terminal_interaction_and_cleanup",
                "tests/test_integrations.py::test_terminal_expiry_and_bounded_output"],
    "mcp":["tests/test_integrations.py::test_mcp_discovery_schema_and_call",
           "tests/test_integrations.py::test_mcp_streamable_http", "tests/test_integrations.py::test_mcp_cancel_does_not_replay"],
    "browser":["tests/test_integrations.py::test_browser_real_form_and_stale_refs"],
    "recovery":["tests/test_local_execution.py::test_cancel_kills_tree_and_preserves_uncertain_action_for_recovery",
                "tests/test_release.py::test_restore_warns_expired_handles_and_never_replays",
                "tests/test_runtime.py::test_unknown_inflight_action_never_replayed"],
}


def main(args):
    folder = ROOT/".kong"/"release-checks"/uuid4().hex[:12]
    folder.mkdir(parents=True)
    report = {"mode":"fixtures+live" if args.live else "fixtures", "cases":[], "live":[]}
    def save():
        (folder/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    for name in args.cases:
        started = time.perf_counter()
        xml = folder/(name+".xml")
        with (folder/(name+".log")).open("w",encoding="utf-8") as log:
            try:
                process = subprocess.run([sys.executable,"-m","pytest",*CASES[name],"-q",f"--junitxml={xml}"],
                    cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=180)
                status = "passed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timeout"
        skipped = sum(int(s.get("skipped",0)) for s in ET.parse(xml).getroot().iter("testsuite")) if xml.exists() else 0
        if status == "passed" and skipped:
            status = "incomplete"
        item = {"case":name,"status":status,"skipped":skipped,"seconds":round(time.perf_counter()-started,3)}
        report["cases"].append(item)
        save()
        print(json.dumps(item),flush=True)
    if args.live:
        for name in args.cases:
            if name == "recovery":
                continue  # Fault injection is deterministic; no pretend model recovery score.
            prefix = "integrations-"+name+"-" if name in {"terminal","mcp","browser"} else name+"-"
            evals = ROOT/".kong"/"evaluations"
            before = set(evals.glob(prefix+"*"))
            script = "integration_evaluation.py" if name in {"terminal","mcp","browser"} else "live_evaluation.py"
            command = [sys.executable,str(ROOT/"examples"/script),"--case",name]
            if name == "research" and args.proxy:
                command += ["--proxy",args.proxy]
            with (folder/(name+"-live.log")).open("w",encoding="utf-8") as log:
                try:
                    result = subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=1200)
                    status = "finished" if result.returncode == 0 else "harness_error"
                except subprocess.TimeoutExpired:
                    status = "timeout"
            candidates = sorted(set(evals.glob(prefix+"*"))-before)
            item = {"case":name,"status":status,"reports":[]}
            for candidate in candidates:
                path = candidate/"evaluation.json"
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    item["reports"].append({"path":str(path),"status":data["status"],"checks":data["checks"],
                        "seconds":data["seconds"],"model_errors":data.get("model_errors",[]),"failed_tools":data.get("failed_tools",[])})
            if status == "finished":
                item["status"] = "passed" if item["reports"] and all(
                    r["status"] == "completed" and r["checks"] and all(r["checks"].values()) for r in item["reports"]) else "failed"
            report["live"].append(item)
            save()
            print(json.dumps(item,ensure_ascii=False),flush=True)
    report["passed"] = all(c["status"] == "passed" for c in report["cases"]+report["live"])
    save()
    print("Report: "+str(folder/"report.json"),flush=True)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true",help="Explicit paid real-model evaluation")
    parser.add_argument("--cases",nargs="+",choices=list(CASES),default=list(CASES))
    parser.add_argument("--proxy",help="Explicit trusted proxy for live research")
    raise SystemExit(main(parser.parse_args()))
