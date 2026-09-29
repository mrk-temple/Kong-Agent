"""Checks performed outside Runtime from actual files and recorded observations."""
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from kong.contracts import Mode, Status
from kong.environments.process import execute_process
from .specs import EVIDENCE_REQUEST


def _csv(path: Path):
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            rows = list(reader)
        return fields, rows
    except (OSError, UnicodeError, csv.Error):
        return None, None


def _actual(runtime, name, operation=None, success=True):
    return [(i, o) for i, o in enumerate(runtime.history.observations)
            if o.action.name == name and (operation is None or o.action.arguments.get("operation") == operation)
            and (success is None or o.success is success)]


def _file(path: Path, expected: str, *, strip=False):
    try:
        content = path.read_text(encoding="utf-8-sig")
        return (content.strip() if strip else content) == expected
    except (OSError, UnicodeError):
        return False


def _runs_script(observation, script):
    argv = observation.action.arguments.get("argv", [])
    if not isinstance(argv, list) or len(argv) < 2:
        return False
    if Path(str(argv[0])).name.lower() not in {"python", "python.exe", "python3", "python3.12", "py", "py.exe", Path(sys.executable).name.lower()}:
        return False
    for arg in argv[1:]:
        if arg in {"-c", "-m"}:
            return False
        if arg in {"-I", "-u", "-B", "-E", "-s", "-S", "-O", "-OO", "-3", "-3.12"}:
            continue
        return Path(str(arg)).name.lower() == script
    return False


def _session_sequence(runtime):
    starts = _actual(runtime, "terminal", "start")
    for start_index, start in starts:
        session = start.output.get("session") if isinstance(start.output, dict) else None
        if not session or not _runs_script(start, "ask.py"):
            continue
        writes = [(i,o) for i,o in _actual(runtime,"terminal","write")
                  if i > start_index and o.action.arguments.get("session") == session
                  and o.action.arguments.get("text", "").strip() == "Kong"]
        reads = [(i,o) for i,o in _actual(runtime,"terminal","read")
                 if writes and i > writes[0][0] and o.action.arguments.get("session") == session
                 and isinstance(o.output,dict) and o.output.get("running") is False and o.output.get("exit_code") == 0]
        closes = [(i,o) for i,o in _actual(runtime,"terminal","close")
                  if reads and i > reads[0][0] and o.action.arguments.get("session") == session]
        if writes and reads and closes:
            return True
    return False


def _data_expected(case_id, workspace):
    if case_id == "data_refunds":
        source = workspace / "ledger.csv"
        _, rows = _csv(source)
        if rows is None:
            return None
        totals = {}
        for row in rows:
            value = Decimal(row["amount"])
            totals[row["product"]] = totals.get(row["product"], Decimal(0)) + (value if row["kind"] == "sale" else -value)
        return ["product", "net_revenue"], [{"product":key, "net_revenue":f"{value:.2f}"} for key, value in sorted(totals.items())]
    source = workspace / ("orders.csv" if case_id == "data_paid_product" else "sales.csv")
    key = "product" if case_id == "data_paid_product" else "region"
    _, rows = _csv(source)
    if rows is None:
        return None
    totals = {}
    for row in rows:
        if row["status"] != "paid":
            continue
        old_q, old_v = totals.get(row[key], (0, Decimal(0)))
        q = int(row["quantity"])
        totals[row[key]] = old_q + q, old_v + q * Decimal(row["unit_price"])
    return [key, "quantity", "revenue"], [{key:k, "quantity":str(q), "revenue":f"{v:.2f}"} for k, (q,v) in sorted(totals.items())]


async def verify(case, workspace: Path, runtime, protected_hashes: dict[str, str], sha256_file) -> dict[str, bool]:
    """All required checks must pass; a Runtime completed label is insufficient."""
    obs = runtime.history.observations
    names = set(case.allowed_tools)
    checks = {
        "allowed_actions": all(o.action.name in names for o in obs),
        "protected_inputs": all((workspace / p).is_file() and sha256_file(workspace / p) == h
                                for p, h in protected_hashes.items()),
    }
    if case.category == "data":
        expected = _data_expected(case.id, workspace) if checks["protected_inputs"] else None
        actual = _csv(workspace / "summary.csv")
        checks.update(exact_csv=expected is not None and actual == expected,
                      script_written=any(o.action.arguments.get("path") == "process.py" for _,o in _actual(runtime,"write_file") + _actual(runtime,"patch_file")),
                      process_after_write=any(i > max((j for j,o in enumerate(obs) if o.success and o.action.name in {"write_file","patch_file"} and o.action.arguments.get("path") == "process.py"), default=-1)
                                              and _runs_script(o, "process.py")
                                              for i,o in _actual(runtime, "process_exec")),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "repair_total":
        try:
            check = await execute_process([sys.executable, "-I", "-c",
                                           "import sys;sys.path.insert(0,'.');from calc import total;assert total([11,-4,2])==9;assert total([])==0;assert total([-9,-1])==-10"],
                                          workspace, timeout_seconds=10, max_output_bytes=2048, encoding="utf-8")
            hidden_pass = check.success
        except (OSError, TimeoutError):
            hidden_pass = False
        original_hash = hashlib.sha256(case.files["calc.py"].encode()).hexdigest()
        edited = (workspace / "calc.py").is_file() and sha256_file(workspace / "calc.py") != original_hash
        last_edit = max((i for i,o in enumerate(obs) if o.action.name in {"write_file","patch_file"}
                         and o.action.arguments.get("path") == "calc.py"), default=-1)
        checks.update(hidden_inputs=hidden_pass, code_modified=edited and last_edit >= 0,
                      check_executed=any(i > last_edit and _runs_script(o, "check.py")
                                         for i,o in _actual(runtime,"process_exec")),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "source_tomllib":
        try:
            answer = json.loads((workspace / "research.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            answer = None
        checks.update(exact_typed_answer=(isinstance(answer, dict) and set(answer) == {"first_version","supports_writing","load_mode","source_file"}
                                          and type(answer.get("first_version")) is str and answer["first_version"] == "3.11"
                                          and type(answer.get("supports_writing")) is bool and answer["supports_writing"] is False
                                          and type(answer.get("load_mode")) is str and answer["load_mode"] == "rb"
                                          and type(answer.get("source_file")) is str and answer["source_file"] == "source.txt"),
                      source_read=bool(_actual(runtime,"read_file") and any(o.action.arguments.get("path") == "source.txt"
                                                                           for _,o in _actual(runtime,"read_file"))),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "terminal_prompt":
        checks.update(exact_answer=_file(workspace / "answer.txt", "Kong"),
                      terminal_sequence=_session_sequence(runtime),
                      no_direct_write=not any(o.action.name in {"write_file","patch_file"} and o.action.arguments.get("path") == "answer.txt" for o in obs),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "mcp_add":
        checks.update(exact_answer=_file(workspace / "result.txt", "42", strip=True),
                      discovered=bool(_actual(runtime,"mcp","servers")) and bool(_actual(runtime,"mcp","list")),
                      actual_call=any(o.action.arguments.get("server") == "acceptance" and o.action.arguments.get("tool") == "add"
                                      and o.action.arguments.get("arguments") == {"a":19,"b":23}
                                      and isinstance(o.output,dict) and o.output.get("structured_content") == {"result":42}
                                      for _,o in _actual(runtime,"mcp","call")),
                      closed=bool(_actual(runtime,"mcp","close")),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "browser_form":
        try:
            saved = json.loads((workspace / "saved.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            saved = None
        proof = workspace / "proof.png"
        browser_sequence = False
        server_sessions = {o.output.get("session") for _,o in _actual(runtime,"terminal","start")
                           if isinstance(o.output,dict) and _runs_script(o, "app.py")}
        terminal_urls = [(i, o.output.get("output", "")) for i,o in _actual(runtime,"terminal") if isinstance(o.output,dict)
                         and o.output.get("session") in server_sessions and o.action.arguments.get("operation") in {"start","read"}]
        for open_i, opened in _actual(runtime,"browser","open"):
            page = opened.output.get("page") if isinstance(opened.output,dict) else None
            url = opened.action.arguments.get("url", "")
            parts = urlsplit(url)
            if not page or parts.scheme != "http" or parts.hostname != "127.0.0.1" or not any(i < open_i and url in output for i,output in terminal_urls):
                continue
            fills = [(i,o) for i,o in _actual(runtime,"browser","fill") if i > open_i and o.action.arguments.get("page") == page and o.action.arguments.get("text") == "Kong"]
            clicks = [(i,o) for i,o in _actual(runtime,"browser","click") if fills and i > fills[0][0] and o.action.arguments.get("page") == page]
            snapshots = [(i,o) for i,o in _actual(runtime,"browser","snapshot") if clicks and i > clicks[0][0]
                         and o.action.arguments.get("page") == page and isinstance(o.output,dict) and "Saved: Kong" in o.output.get("text","")]
            shots = [(i,o) for i,o in _actual(runtime,"browser","screenshot") if clicks and i > clicks[0][0] and o.action.arguments.get("page") == page and o.action.arguments.get("path") == "proof.png"]
            closes = [(i,o) for i,o in _actual(runtime,"browser","close") if snapshots and shots and i > max(snapshots[0][0],shots[0][0]) and o.action.arguments.get("page") == page]
            if fills and clicks and snapshots and shots and closes:
                browser_sequence = True
                break
        checks.update(backend_state=saved == {"name":"Kong"},
                      browser_sequence=browser_sequence,
                      screenshot=proof.is_file() and proof.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
                      browser_closed=bool(_actual(runtime,"browser","close")),
                      terminal_closed=bool(server_sessions) and all(any(o.action.arguments.get("session") == session for _,o in _actual(runtime,"terminal","close")) for session in server_sessions),
                      no_direct_write=not any(o.action.name in {"write_file","patch_file"} and o.action.arguments.get("path") == "saved.json" for o in obs),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "tool_error_recovery":
        failed = [(i,o) for i,o in _actual(runtime,"read_file",success=False) if o.action.arguments.get("path") == "missing.txt"]
        recovered = [(i,o) for i,o in _actual(runtime,"read_file") if o.action.arguments.get("path") == "result.txt"]
        checks.update(first_failure_recorded=bool(failed), later_verified=bool(failed and recovered and recovered[-1][0] > failed[0][0]),
                      exact_answer=_file(workspace / "result.txt", "RECOVERED"),
                      runtime_completed=runtime.state.status == Status.COMPLETED)
    elif case.id == "unknown_action_restore":
        checks.update(waiting_for_resolution=runtime.state.status == Status.WAITING_USER and bool(runtime.pending_action_id),
                      no_replay=len(obs) == 0 and not (workspace / "canary.txt").exists(),
                      no_model_call=runtime.metrics.model_calls == 0)
    elif case.id == "plan_approval":
        checks.update(expected_approval=runtime.state.mode == Mode.PLAN and runtime.state.status == Status.WAITING_USER
                      and runtime.state.pending_approval is not None and runtime.state.plan is not None
                      and runtime.state.plan.approved_revision is None,
                      no_actions=len(obs) == 0 and not (workspace / "output.txt").exists())
    elif case.id == "insufficient_evidence":
        checks.update(source_inspected=any(o.action.arguments.get("path") == "claim.txt" for _,o in _actual(runtime,"read_file")),
                      asked_for_evidence=runtime.state.status == Status.WAITING_USER and runtime.state.pending_question == EVIDENCE_REQUEST
                      and not runtime.state.final_output,
                      no_report=not (workspace / "report.txt").exists() and not (workspace / "report.md").exists())
    return checks
