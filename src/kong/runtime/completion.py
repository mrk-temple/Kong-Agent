"""Read-only deterministic validation of host completion requirements."""
import csv
import io
import json
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from kong.contracts import Criterion
from kong.runtime.evidence import fingerprint, fresh


def stable_bytes(workspace, path):
    resolved = workspace.resolve(path)
    before = resolved.stat()
    if not resolved.is_file() or before.st_size > 1_000_000:
        raise ValueError("Missing file or contract verification size limit")
    data = resolved.read_bytes()
    after = resolved.stat()
    if len(data) > 1_000_000 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("File changed during validation")
    return data


def csv_rows(data):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    columns = reader.fieldnames
    if not columns or len(columns) != len(set(columns)) or any(not name for name in columns):
        raise ValueError("Missing or duplicate CSV columns")
    rows = list(reader)
    if len(rows) > 100_000 or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError("CSV row shape or size mismatch")
    return columns, rows


def field_matches(value, requirement):
    expected = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list,
                "array_string": list, "array_integer": list, "null": type(None)}
    if requirement.type == "number":
        valid = type(value) in {int, float} and (not isinstance(value, float) or math.isfinite(value))
    else:
        valid = type(value) is expected[requirement.type]
    if valid and requirement.type in {"array_string", "array_integer"}:
        subtype = str if requirement.type == "array_string" else int
        valid = all(type(item) is subtype for item in value)
    if valid and requirement.allowed:
        from kong.runtime.gates import exact_json
        values = value if requirement.type.startswith("array") else [value]
        valid = all(any(exact_json(item, allowed) for allowed in requirement.allowed) for item in values)
    return valid


def read_evidence(path, state, history, workspace, *, final=False):
    candidates = []
    for index, obs in enumerate(history.observations):
        if obs.action.name != "read_file":
            continue
        try:
            matches = workspace.resolve(obs.action.arguments.get("path", "")) == workspace.resolve(path)
        except (OSError, ValueError):
            matches = False
        if matches:
            candidates.append((index, obs))
    if not candidates:
        return []
    index, obs = candidates[-1]
    current = fingerprint(workspace, path)
    if (not obs.success or obs.outcome_unknown or obs.evidence_version != 2 or current in {None, "missing"}
            or obs.artifact_versions.get(path) != current or obs.artifact_versions_before.get(path) != current):
        return []
    if final:
        try:
            text = stable_bytes(workspace, path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            output = obs.output
            if isinstance(output, dict):
                if output.get("offset") != 0 or output.get("truncated") is not False:
                    return []
                output = output.get("content")
            if output != text:
                return []
        except (OSError, ValueError, UnicodeError):
            return []
    if final and not fresh(Criterion(id="readback", description="Final artifact readback", artifact_paths=[path]),
                           history, index, workspace, state):
        return []
    return [obs.action_id]


def ask_problems(state, decision, history, workspace):
    contract = state.completion_contract
    if contract is None:
        return []
    # A prerequisite must never force actions before PLAN approval.
    if state.pending_approval or state.mode.value == "plan" and (not state.plan or state.plan.approved_revision != state.plan.revision):
        return []
    problems = [f"Read {path} successfully before this task's evidence request."
                for path in contract.read_before_ask if not read_evidence(path, state, history, workspace)]
    if contract.required_question is not None and decision.question != contract.required_question:
        problems.append("Use the exact evidence-request question declared in the host contract.")
    return problems


def aggregate_matches(rule, artifact, workspace):
    columns, rows = csv_rows(stable_bytes(workspace, rule.source))
    if any(key not in columns for key in rule.filters) or rule.column is not None and rule.column not in columns:
        return False
    rows = [row for row in rows if all(row[key] in values for key, values in rule.filters.items())]
    actual = artifact.get(rule.field)
    if rule.operation == "count":
        return type(actual) is int and actual == len(rows)
    if rule.operation in {"unique_values", "sorted_values"}:
        values = [row[rule.column] for row in rows]
        expected = sorted(set(values)) if rule.operation == "unique_values" else sorted(values)
        return type(actual) is list and all(type(v) is str for v in actual) and actual == expected
    numbers = [Decimal(row[rule.column]) for row in rows]
    if any(not value.is_finite() or abs(value.adjusted()) > 128 for value in numbers):
        return False
    total = sum(numbers, Decimal(0))
    if rule.decimal_places is not None:
        expected = format(total.quantize(Decimal(1).scaleb(-rule.decimal_places), rounding=ROUND_HALF_UP),
                          f".{rule.decimal_places}f")
        return type(actual) is str and actual == expected
    return type(actual) in {int, float} and Decimal(str(actual)) == total


def contract_results(state, history, workspace):
    contract = state.completion_contract
    if contract is None:
        return []
    results, parsed = [], {}
    def add(id_, kind, valid, description, ids=None):
        results.append({"scope": "contract", "id": id_, "kind": kind, "verified": bool(valid),
                        "description": description, "evidence_ids": ids or []})
    add("contract:binding", "binding", not state.contract_invalidated, "Host contract invalidated by changed goal or constraints")
    add("contract:completion", "completion", contract.allow_completion, "This task permits a completion response")
    initial_versions = {path: fingerprint(workspace, path) for path in contract.version_paths()}
    for path in contract.protected_paths:
        expected = state.contract_protected_versions.get(path)
        add("protected:" + path, "protected", expected not in {None, "missing"} and initial_versions[path] == expected,
            "Protected source: " + path)
    for path in contract.required_reads:
        ids = read_evidence(path, state, history, workspace)
        add("read:" + path, "read", bool(ids), "Required source read: " + path, ids)
    from kong.runtime.gates import exact_json, invalid_constant, unique_object
    for artifact in contract.artifacts:
        valid = False
        try:
            data = stable_bytes(workspace, artifact.path)
            valid = True
            if artifact.format == "text":
                text = data.decode("utf-8")
                valid = artifact.expected_text is None or text == artifact.expected_text
            elif artifact.format == "json":
                value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
                parsed[artifact.id] = value
                valid = type(value) is dict
                if valid:
                    valid = all(f.name in value and field_matches(value[f.name], f) for f in artifact.json_fields)
                    if not artifact.additional_keys:
                        valid = valid and set(value) == {f.name for f in artifact.json_fields}
                    if artifact.exact_json is not None:
                        valid = valid and exact_json(value, artifact.exact_json)
            elif artifact.format == "csv":
                columns, rows = csv_rows(data)
                valid = (not artifact.csv_columns or columns == artifact.csv_columns)
                valid = valid and (artifact.row_count is None or len(rows) == artifact.row_count)
                valid = valid and (artifact.csv_rows is None or rows == artifact.csv_rows)
        except (OSError, ValueError, UnicodeError, RecursionError):
            valid = False
        add("artifact:" + artifact.id, "artifact", valid, "Final artifact structure/value: " + artifact.path)
        if artifact.require_readback:
            ids = read_evidence(artifact.path, state, history, workspace, final=True)
            add("readback:" + artifact.id, "readback", bool(ids), "Current final readback: " + artifact.path, ids)
    for rule in contract.aggregates:
        valid = False
        try:
            artifact = parsed.get(rule.artifact_id)
            valid = type(artifact) is dict and aggregate_matches(rule, artifact, workspace)
        except (OSError, ValueError, UnicodeError, InvalidOperation, OverflowError):
            pass
        add("aggregate:" + rule.id, "aggregate", valid, "Business aggregate: " + rule.id)
    for command in contract.commands:
        candidates = []
        for index, obs in enumerate(history.observations):
            if obs.action.name != "process_exec" or obs.action.arguments.get("argv") != command.argv:
                continue
            try:
                same_cwd = workspace.resolve(obs.action.arguments.get("cwd", ".")) == workspace.resolve(command.cwd)
            except (OSError, ValueError):
                same_cwd = False
            if same_cwd:
                candidates.append((index, obs))
        valid, ids = False, []
        if candidates:
            index, obs = candidates[-1]
            output = obs.output if isinstance(obs.output, dict) else {}
            stdout = output.get("stdout")
            valid = (not obs.outcome_unknown and not output.get("timed_out") and type(output.get("exit_code")) is int
                     and output["exit_code"] == command.exit_code and isinstance(stdout, str)
                     and all(text in stdout for text in command.stdout_contains))
            if command.stdout_exact is not None:
                valid = valid and not output.get("stdout_truncated") and stdout == command.stdout_exact
            if command.fresh_python:
                valid = valid and obs.action.arguments.get("fresh_python") is True and output.get("fresh_python_imports") is True
            if valid and command.verify_final:
                criterion = Criterion(id="command", description="Final command verification", kind="tool_success",
                    tool_name="process_exec", after_last_write=True, artifact_paths=contract.version_paths())
                valid = fresh(criterion, history, index, workspace, state)
            if valid:
                ids = [obs.action_id]
        add("command:" + command.id, "command", valid, "Required command/exit/output: " + command.id, ids)
    # Detect external changes across the entire read-only validation window.
    stable = all(value is not None and value == fingerprint(workspace, path) for path, value in initial_versions.items())
    add("contract:stable", "version", stable, "Contract files stable throughout final validation")
    return results
