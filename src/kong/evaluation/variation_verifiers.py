"""Independent, fixed checks for the V0.4A variation contracts."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

from kong.contracts import Status
from kong.environments.process import execute_process

from .variations import CONTRACTS
from .verifiers import _csv, _file, _runs_script


HIDDEN_DATA = {
    "v04_data_month": ("orders.csv", "date,amount,status\n2025-12-31,0.01,paid\n2026-01-01,9.99,paid\n2025-12-08,3.00,void\n2025-12-02,1.25,paid\n"),
    "v04_data_inventory": ("movements.csv", "sku,kind,quantity\nZ,out,2\nA,in,10\nZ,in,9\nA,out,11\n"),
    "v04_data_dedupe": ("events.csv", "order_id,customer,status,amount\n1,C,paid,9.00\n2,A,paid,2.25\n1,C,paid,3.10\n2,A,void,2.25\n3,B,paid,0.05\n"),
    "v04_data_tax": ("invoices.csv", "region,net,tax_rate,status\nSouth,0.05,0.10,paid\nSouth,0.05,0.10,paid\nNorth,2.00,0.05,paid\nNorth,99.00,0.00,void\n"),
}


HIDDEN_REPAIR = {
    "v04_repair_clamp": "from limits import clamp\nassert clamp(12,0,10)==10\nassert clamp(-8,-5,-2)==-5\nassert clamp(-3,-5,-2)==-3\n",
    "v04_repair_mean": "from stats import mean\nassert mean([1,2])==1.5\nassert mean([-2,0,2])==0\nassert mean([7])==7\nassert mean([]) is None\n",
    "v04_repair_slug": "from textutil import slug\nassert slug(' A\\t B\\nC ')=='a-b-c'\nassert slug('  ') == ''\n",
    "v04_repair_merge": "from counts import merge\na={'x':4,'z':-2};b={'x':-1,'y':3};assert merge(a,b)=={'x':3,'y':3,'z':-2};assert a=={'x':4,'z':-2};assert b=={'x':-1,'y':3}\n",
    "v04_continue_export": "from export import export_rows\nassert export_rows([(3,'C'),(1,'A'),(2,'B')])=='id,name\\n1,A\\n2,B\\n3,C\\n'\nassert export_rows([])=='id,name\\n'\n",
    "v04_continue_failures": "from parser import parse_int\nassert parse_int(' 0 ')==0\nassert parse_int('+12')==12\ntry:parse_int('x')\nexcept ValueError:pass\nelse:raise AssertionError('invalid text accepted')\n",
}


def _expected_data(case_id: str, source: Path):
    _, rows = _csv(source)
    if rows is None:
        return None
    if case_id == "v04_data_month":
        totals = defaultdict(lambda: Decimal("0"))
        for row in rows:
            if row["status"] == "paid":
                totals[row["date"][:7]] += Decimal(row["amount"])
        return ["month", "paid_revenue"], [{"month":key,"paid_revenue":f"{value:.2f}"} for key,value in sorted(totals.items())]
    if case_id == "v04_data_inventory":
        totals = defaultdict(int)
        for row in rows:
            totals[row["sku"]] += int(row["quantity"]) * (1 if row["kind"] == "in" else -1)
        return ["sku", "stock"], [{"sku":key,"stock":str(value)} for key,value in sorted(totals.items())]
    if case_id == "v04_data_dedupe":
        latest = {row["order_id"]:row for row in rows}
        totals = defaultdict(lambda: Decimal("0"))
        for row in latest.values():
            if row["status"] == "paid":
                totals[row["customer"]] += Decimal(row["amount"])
        return ["customer", "paid_total"], [{"customer":key,"paid_total":f"{value:.2f}"} for key,value in sorted(totals.items())]
    if case_id == "v04_data_tax":
        totals = defaultdict(lambda: Decimal("0"))
        for row in rows:
            if row["status"] == "paid":
                gross = Decimal(row["net"]) * (Decimal("1") + Decimal(row["tax_rate"]))
                totals[row["region"]] += gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ["region", "gross"], [{"region":key,"gross":f"{value:.2f}"} for key,value in sorted(totals.items())]
    raise ValueError("Unknown variation data case")


async def _hidden_check(case_id: str, workspace: Path, artifact: str) -> bool:
    try:
        with TemporaryDirectory(prefix="kong-v04a-check-") as location:
            hidden = Path(location)
            shutil.copyfile(workspace / artifact, hidden / artifact)
            if case_id in HIDDEN_DATA:
                name, content = HIDDEN_DATA[case_id]
                (hidden / name).write_text(content, encoding="utf-8")
                result = await execute_process([sys.executable, "-I", artifact], hidden,
                                               timeout_seconds=10, max_output_bytes=2048, encoding="utf-8")
                return result.success and _csv(hidden / "summary.csv") == _expected_data(case_id, hidden / name)
            script = "import sys;sys.path.insert(0,'.');" + HIDDEN_REPAIR[case_id]
            result = await execute_process([sys.executable, "-I", "-c", script], hidden,
                                           timeout_seconds=10, max_output_bytes=2048, encoding="utf-8")
            return result.success
    except (OSError, TimeoutError, ValueError):
        return False


def _typed_report(case_id: str, workspace: Path) -> bool:
    try:
        report = json.loads((workspace / "report.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return False
    if case_id == "v04_revision_status":
        _, rows = _csv(workspace / "rows.csv")
        return isinstance(report, dict) and set(report) == {"included_statuses", "total"} and report["included_statuses"] == ["active"] and type(report["total"]) is int and report["total"] == sum(int(row["value"]) for row in rows if row["status"] == "active")
    if case_id == "v04_revision_currency":
        _, rows = _csv(workspace / "amounts.csv")
        amount = sum((Decimal(row["amount"]) for row in rows if row["currency"] == "USD"), Decimal("0"))
        return isinstance(report, dict) and set(report) == {"currency", "total"} and report["currency"] == "USD" and type(report["total"]) is str and report["total"] == f"{amount:.2f}"
    if case_id == "v04_revision_schema":
        _, rows = _csv(workspace / "users.csv")
        return isinstance(report, dict) and set(report) == {"active_names"} and report["active_names"] == sorted(row["name"] for row in rows if row["status"] == "active")
    return False


async def verify_variation(case, workspace: Path, runtime, protected_hashes: dict[str,str], sha256_file,
                           *, prior_run_id: str | None = None, prior_protocol: str = "seeded",
                           readback_policy: str = "direct") -> dict[str,bool]:
    if readback_policy not in {"direct", "protected_check"}:
        raise ValueError("Unknown readback policy")
    contract = CONTRACTS[case.id]
    obs = runtime.history.observations
    checks = {
        "allowed_actions": all(o.action.name in case.allowed_tools for o in obs),
        "protected_inputs": all((workspace / name).is_file() and sha256_file(workspace / name) == value
                                for name,value in protected_hashes.items()),
        "runtime_completed": runtime.state.status == Status.COMPLETED,
        "artifact_exists": (workspace / contract.artifact).is_file(),
    }
    reads = [(i,o.action.arguments.get("path")) for i,o in enumerate(obs) if o.success and o.action.name == "read_file"]
    writes = [(i,o.action.arguments.get("path")) for i,o in enumerate(obs) if o.success and o.action.name in {"write_file","patch_file"}]
    checks["sources_read"] = all(any(path == required for _,path in reads) for required in contract.required_reads if required != contract.artifact)
    if contract.required_failed_read:
        failures = [i for i,o in enumerate(obs) if not o.success and o.action.name == "read_file" and o.action.arguments.get("path") == contract.required_failed_read]
        checks["failure_recorded"] = bool(failures)
        checks["recovered_after_failure"] = bool(failures and any(i > failures[0] and path == contract.artifact for i,path in reads))
    if contract.required_failed_process:
        failed_runs = [i for i,o in enumerate(obs) if not o.success and o.action.name == "process_exec"
                       and o.action.arguments.get("argv") == list(contract.required_failed_process)]
        checks["failure_recorded"] = bool(failed_runs)
        checks["recovered_after_failure"] = bool(failed_runs and any(i > failed_runs[0] and path == contract.artifact for i,path in reads))
    if case.category == "data_v04":
        checks["artifact_read_after_write"] = any(i > j and path == contract.artifact for j,o in enumerate(obs)
                                                  if o.success and o.action.name == "process_exec" and _runs_script(o, "process.py")
                                                  for i,path in reads)
    elif contract.artifact not in contract.editable:
        checks["artifact_read_after_write"] = any(i > j and path == contract.artifact for j,wpath in writes
                                                  if wpath == contract.artifact for i,path in reads)
    else:
        checks["artifact_read_after_write"] = True
    if contract.required_process:
        last_edit = max((i for i,path in writes if path == contract.editable[0]), default=-1)
        checks["script_edited"] = last_edit >= 0
        checks["process_after_edit"] = any(i > last_edit and _runs_script(o, contract.required_process)
                                           for i,o in enumerate(obs) if o.success and o.action.name == "process_exec")
        if case.category == "data_v04":
            last_generation = max((i for i,o in enumerate(obs) if o.success and o.action.name == "process_exec"
                                   and _runs_script(o, "process.py")), default=-1)
            checks["final_check_after_generation"] = any(i > last_generation and _runs_script(o, "check.py")
                                                          for i,o in enumerate(obs) if o.success and o.action.name == "process_exec")
            if readback_policy == "protected_check":
                checks["artifact_read_after_write"] = (checks["artifact_read_after_write"] or
                    checks["protected_inputs"] and checks["final_check_after_generation"] and last_generation >= 0)
        checks["hidden_inputs"] = await _hidden_check(case.id, workspace, contract.editable[0]) if checks["artifact_exists"] else False
    if case.category == "data_v04":
        input_name = HIDDEN_DATA[case.id][0]
        checks["exact_csv"] = checks["protected_inputs"] and _csv(workspace / "summary.csv") == _expected_data(case.id, workspace / input_name)
    elif case.category == "recovery_v04":
        if case.id == "v04_recover_missing":
            value = (workspace / "instructions.txt").read_text(encoding="utf-8").split("=",1)[1].strip()
            checks["exact_result"] = _file(workspace / "result.txt", value, strip=True)
        elif case.id == "v04_recover_absent":
            value = (workspace / "recipe.txt").read_text(encoding="utf-8").split("=",1)[1].strip()
            checks["exact_result"] = _file(workspace / "report.txt", value, strip=True)
        else:
            try:
                value = json.loads((workspace / "canonical.json").read_text(encoding="utf-8"))["value"]
                actual = json.loads((workspace / "result.json").read_text(encoding="utf-8"))
                checks["exact_result"] = type(actual) is str and actual == value
            except (OSError, UnicodeError, ValueError, KeyError):
                checks["exact_result"] = False
    elif case.category == "revision_v04":
        checks["latest_requirement_first"] = any(i < j for i,path in reads if path in {"correction.md","change.md","update.md"} for j,wpath in writes if wpath == "report.json")
        checks["exact_revised_report"] = checks["protected_inputs"] and _typed_report(case.id, workspace)
    if contract.continuation:
        try:
            previous = runtime.store.load(prior_run_id) if prior_run_id else None
            associated = bool(previous and previous.thread_id == runtime.thread.thread_id)
            if prior_protocol == "executed":
                failed_check = any(not o.success and o.action.name == "process_exec" and _runs_script(o, "check.py")
                                   for o in previous.history.observations) if previous else False
                paused = bool(previous and previous.state.status == Status.WAITING_USER and previous.state.pending_question)
                checks["prior_run_preserved"] = associated and paused and failed_check
            else:
                checks["prior_run_preserved"] = associated and previous.state.status == Status.COMPLETED and any(e.type == "human" for e in previous.history.events)
        except (OSError, ValueError):
            checks["prior_run_preserved"] = False
    return checks
