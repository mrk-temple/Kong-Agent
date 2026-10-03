"""Kong-Eval: one immutable trial directory per case and repetition."""
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import time
from uuid import uuid4

import httpx

import kong
from kong.config import ModelConfig
from kong.contracts import Act, Action, Criterion, NeedUserInput, Status
from kong.integrations.config import MCPServer
from kong.models.compatible import CompatibleModel
from kong.models.fake import ScriptedModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace

from .fixtures import FixtureBrowser, FixtureMCP, FixtureTerminal, fixture_model, legacy_a3_fixture_model
from .specs import CASES, Case
from .verifiers import verify
from .variation_fixtures import variation_fixture_model
from .variation_verifiers import verify_variation
from .variations import CONTRACTS, VARIATION_CASES
from .audit import archive_fixture_trial
from .plan_variations import PLAN_CASES

EVAL_SCHEMA = "kong-eval-1"
CASE_SETS = {"legacy12": CASES, "legacy12_a3": CASES,
             "legacy12_contracts": CASES,
             "variation16": VARIATION_CASES, "variation16_handoff": VARIATION_CASES,
             "variation16_v2": VARIATION_CASES, "plan3": PLAN_CASES}
CASE_SETS["variation16_contracts"] = VARIATION_CASES


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _implementation_sha256() -> str:
    package = Path(__file__).resolve().parents[1]
    source = [(path.relative_to(package).as_posix(), sha256_file(path)) for path in sorted(package.rglob("*.py"))]
    entry = Path(__file__).resolve().parents[3] / "examples" / "kong_eval.py"
    if entry.exists():
        source.append(("examples/kong_eval.py", sha256_file(entry)))
    return _digest(source)


def _behavior_resources() -> dict[str, str]:
    """Fingerprint code, prompts, built-in skills, references and verifier inputs."""
    root = Path(__file__).resolve().parents[3]
    selected = [path for path in (root / "src" / "kong").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    selected += [root / name for name in ("examples/kong_eval.py", "examples/export_eval_report.py",
                                         "pyproject.toml", "uv.lock", "kong.example.toml")]
    selected += list((root / "tests").glob("test_evaluation*.py"))
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(set(selected)) if path.is_file()}


def _write_json(path: Path, data) -> None:
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    pending.replace(path)


def manifest(case: Case) -> dict:
    result = {
        "id": case.id, "category": case.category, "goal": case.goal,
        "initial_environment": {"files": case.files, "fresh_workspace": True, "fresh_runtime_store": True,
                                "fresh_model_client": True, "fresh_tool_registry": True},
        "allowed_tools": list(case.allowed_tools), "success_condition": case.success,
        "time_budget_seconds": case.timeout_seconds, "turn_budget": case.turn_limit,
        "mode": case.mode.value, "score_scope": case.score_scope,
        "source_scope": "fixed_local_snapshot_only" if case.id == "source_tomllib" else None,
    }
    if case.id in CONTRACTS:
        contract = CONTRACTS[case.id]
        result["verification"] = {"artifact":contract.artifact, "editable_inputs":list(contract.editable),
                                  "required_reads":list(contract.required_reads),
                                  "required_failed_read":contract.required_failed_read,
                                  "required_failed_process":list(contract.required_failed_process) if contract.required_failed_process else None,
                                  "required_process":contract.required_process,
                                  "hidden_inputs":contract.hidden_inputs,
                                  "seeded_prior_run":contract.continuation}
    return result


class MeteredModel(CompatibleModel):
    """Collect only metadata; never persist prompts, responses or credentials."""

    def __init__(self, config: ModelConfig, client: httpx.AsyncClient):
        super().__init__(config, client)
        self.calls = []

    async def generate(self, context):
        started = time.perf_counter()
        item = {"number":len(self.calls) + 1, "seconds":None, "usage":None, "outcome":"error"}
        try:
            decision = await super().generate(context)
            item["outcome"] = "decision"
            item["decision_kind"] = decision.kind
            return decision
        finally:
            item["seconds"] = round(time.perf_counter() - started, 3)
            item["usage"] = self.last_usage or None
            self.calls.append(item)


def _criteria(case: Case, task_set="legacy12"):
    if task_set == "variation16_contracts":
        result = _criteria(case, "variation16_v2")
        if case.category == "recovery_v04":
            contract = CONTRACTS[case.id]
            if case.id == "v04_recover_invalid":
                expected = json.dumps(json.loads(case.files["canonical.json"])["value"])
            else:
                source = "instructions.txt" if case.id == "v04_recover_missing" else "recipe.txt"
                expected = case.files[source].split("=", 1)[1].strip()
            result.append(Criterion(id="exact_recovered_value", description="Declared recovered value contract; expected value visible",
                kind="file_equals", path=contract.artifact, expected_text=expected))
        return result
    if task_set == "legacy12_contracts":
        result = _criteria(case, "legacy12_a3")
        if case.id == "source_tomllib":
            result.append(Criterion(id="typed_output", description="Declared canonical JSON output contract; expected facts visible",
                kind="file_json", path="research.json", json_match={"first_version":"3.11", "supports_writing":False,
                "load_mode":"rb", "source_file":"source.txt"}))
        if case.id == "mcp_add":
            result.append(Criterion(id="plain_result", description="Save the result alone, without a JSON wrapper or whitespace",
                kind="file_equals", path="result.txt", expected_text="42"))
        return result
    if case.id in CONTRACTS:
        contract = CONTRACTS[case.id]
        result = [Criterion(id="artifact", description="Requested artifact exists", kind="file_exists", path=contract.artifact)]
        if contract.required_process:
            artifacts = [*contract.editable, contract.artifact]
            if contract.artifact in contract.editable:
                artifacts.append(contract.required_process)
            result.append(Criterion(id="execution", description="Program ran after edit", kind="tool_success",
                                    tool_name="process_exec", after_last_write=True,
                                    artifact_paths=list(dict.fromkeys(artifacts))))
        return result
    output = {"data":"summary.csv", "repair":"calc.py", "source_research":"research.json",
              "terminal":"answer.txt", "mcp":"result.txt", "browser":"saved.json", "tool_error":"result.txt"}.get(case.category)
    result = [Criterion(id="artifact", description="Requested file exists", kind="file_exists", path=output)] if output else []
    if case.category in {"data", "repair"}:
        artifacts = ["process.py", "summary.csv"] if case.category == "data" else ["calc.py", "check.py"]
        result.append(Criterion(id="execution", description="Program ran after edit", kind="tool_success",
                                tool_name="process_exec", after_last_write=True,
                                artifact_paths=artifacts if task_set == "legacy12_a3" else []))
    if case.id == "terminal_prompt":
        result.append(Criterion(id="exit", description="Terminal exit read", kind="tool_result", tool_name="terminal",
                                operation="read", output_match={"running":False,"exit_code":0}))
    if case.id == "mcp_add":
        result.append(Criterion(id="mcp", description="Real MCP tool result", kind="tool_result", tool_name="mcp",
                                operation="call", arguments_match={"server":"acceptance","tool":"add","arguments":{"a":19,"b":23}},
                                output_match={"structured_content":{"result":42}}))
    if case.id == "browser_form":
        result.extend((Criterion(id="dom", description="Saved value visible", kind="tool_result", tool_name="browser",
                                 operation="snapshot", output_contains={"/text":"Saved: Kong"}),
                       Criterion(id="png", description="Screenshot exists", kind="file_exists", path="proof.png")))
    if task_set == "legacy12_a3":
        for criterion in result:
            if criterion.kind == "tool_result" and criterion.tool_name in {"browser", "mcp", "terminal"}:
                # These legacy contracts ask for an observed operation, not a
                # guarantee about a remote resource after closing its connection.
                # Final local outputs still undergo independent verification.
                criterion.evidence_scope = "event"
    return result


def _missing_dependency(case: Case) -> str | None:
    required = []
    if "terminal" in case.allowed_tools:
        required.append("winpty")
    if "mcp" in case.allowed_tools:
        required.extend(("mcp", "jsonschema"))
    if "browser" in case.allowed_tools:
        required.append("playwright")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if platform.system() != "Windows" and "terminal" in case.allowed_tools:
        return "terminal_requires_windows"
    if missing:
        return "missing_optional_dependency:" + ",".join(missing)
    if "browser" in case.allowed_tools:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                if not Path(playwright.chromium.executable_path).is_file():
                    return "missing_chromium_binary"
        except Exception:
            return "playwright_preflight_failed"
    return None


def _registry(case: Case, workspace: Workspace, fixture: bool):
    if fixture:
        full = default_registry(workspace, allow_process="process_exec" in case.allowed_tools)
        extras = {"terminal":FixtureTerminal(workspace), "mcp":FixtureMCP(), "browser":FixtureBrowser(workspace)}
    else:
        server = {"acceptance": MCPServer(command="python", args=[str(workspace.root / "server.py")])} if case.id == "mcp_add" else None
        full = default_registry(workspace, allow_process="process_exec" in case.allowed_tools,
                                allow_terminal="terminal" in case.allowed_tools,
                                allow_browser="browser" in case.allowed_tools, mcp_servers=server)
        extras = {}
    limited = ToolRegistry()
    for name in case.allowed_tools:
        limited.register(extras[name] if name in extras else full.get(name))
    return limited


def _status(case: Case, runtime, checks, *, timed_out=False, environment_error=None):
    if timed_out:
        return "timeout"
    if environment_error:
        return "environment_error"
    if checks and all(checks.values()):
        return "expected_approval" if case.category == "approval" else "success"
    errors = [e.payload.get("error", "") for e in runtime.history.events if e.type == "model_error"]
    if any("Model service HTTP" in e or "Cannot connect" in e or "Model request timed out" in e for e in errors):
        return "environment_error"
    return "failed"


def _model_error_categories(runtime):
    categories = []
    for event in runtime.history.events:
        if event.type != "model_error":
            continue
        value = str(event.payload.get("error", ""))
        if event.payload.get("category"):
            category = event.payload["category"]
        elif "Model service HTTP" in value:
            category = "service_http"
        elif "Cannot connect" in value:
            category = "connection"
        elif "timed out" in value:
            category = "timeout"
        elif "Invalid Kong decision JSON" in value:
            category = "protocol"
        else:
            category = "other"
        categories.append(category)
    return categories


def _tool_failures(runtime):
    failures = []
    for observation in runtime.history.observations:
        if observation.success:
            continue
        error = (observation.error or "").lower()
        category = ("dependency" if any(word in error for word in ("dependency", "module", "winpty", "playwright"))
                    else "timeout" if "timed out" in error or "timeout" in error
                    else "missing" if "not found" in error or "missing" in error
                    else "rejected" if "invalid" in error or "requires" in error
                    else "other")
        failures.append({"tool":observation.action.name,
                         "operation":observation.action.arguments.get("operation"), "error_category":category})
    return failures


def _safe_exception_location(exc: Exception, stage: str) -> dict:
    """Keep only application code location and numeric OS error identifiers."""
    package_root = Path(__file__).resolve().parents[1]
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        path = Path(trace.tb_frame.f_code.co_filename)
        try:
            relative = path.resolve().relative_to(package_root)
        except ValueError:
            pass
        else:
            frames.append({"module":"kong." + relative.with_suffix("").as_posix().replace("/", "."),
                           "function":trace.tb_frame.f_code.co_name, "line":trace.tb_lineno})
        trace = trace.tb_next
    return {"stage":stage, "exception_type":type(exc).__name__,
            "errno":getattr(exc, "errno", None), "winerror":getattr(exc, "winerror", None),
            "code_location":frames[-1] if frames else None}


def _summary(results, mode, suite_id, comparison_id, implementation_sha256, task_set, resource_fingerprint):
    counts = {s:sum(r["status"] == s for r in results) for s in
              ("success","failed","expected_approval","environment_error","timeout")}
    scored = [r for r in results if r["score_scope"] == "model"] if mode == "live" else []
    eligible = [r for r in scored if r["status"] not in {"environment_error"}]
    claims = [r for r in results if r["score_scope"] == "model" and r["runtime_status"] == "completed"]
    incorrect = sum(r["error_completion"] for r in claims)
    observed_usage = [r["usage"] for r in results if isinstance(r["usage"], dict)]
    usage_keys = sorted({key for usage in observed_usage for key in usage})
    usage = {key:sum(item.get(key, 0) for item in observed_usage) for key in usage_keys} if observed_usage else None
    return {"schema":EVAL_SCHEMA, "suite_id":suite_id, "mode":mode, "comparison_id":comparison_id,
            "task_set":task_set, "implementation_sha256":implementation_sha256,
            "resource_fingerprint":resource_fingerprint,
            "runs":len(results), "counts":counts,
            "fixture_harness_passes":sum(r["status"] in {"success","expected_approval"} for r in results) if mode == "fixtures" else None,
            "model_scored_runs":len(eligible) if mode == "live" else None,
            "model_successes":sum(r["status"] in {"success","expected_approval"} for r in eligible) if mode == "live" else None,
            "model_score":(sum(r["status"] in {"success","expected_approval"} for r in eligible) / len(eligible)) if eligible else None,
            "environment_exclusions":sum(r["status"] == "environment_error" for r in scored),
            "model_attempts":len(scored) if mode == "live" else None,
            "model_pass_rate_all_attempts":(sum(r["status"] in {"success","expected_approval"} for r in scored) / len(scored)) if scored else None,
            "mechanism_only_runs":sum(r["score_scope"] == "mechanism" for r in results),
            "error_completion_count":sum(r["error_completion"] for r in results),
            "completed_claims":len(claims),
            "error_completion_rate":incorrect / len(claims) if claims else None,
            "elapsed_seconds_total":round(sum(r["elapsed_seconds"] for r in results), 3),
            "actual_usage":usage,
            "usage_complete":(all(r["usage_complete"] is True for r in results if r["model_calls"] > 0)
                              if mode == "live" and any(r["model_calls"] > 0 for r in results) else None),
            "completion_rate_scope":"Model-scored trials in the stated mode; mechanism-only trials excluded. Fixture claims do not measure model reliability.",
            "human_intervention":{"measured":False,"count":None,"corrections":None,"rescues":None,
                                  "note":"Automatic trials have no observed human supervision burden; unknown is not zero."},
            "results":[{"case":r["case"], "repetition":r["repetition"], "status":r["status"],
                        "model_calls":r["model_calls"], "result":r["result_path"]} for r in results]}


async def _trial(case: Case, repeat: int, folder: Path, *, fixture: bool, config: ModelConfig | None,
                 comparison_id: str, suite_id: str, profile: str | None, implementation_sha256: str,
                 task_set: str, resource_fingerprint: str):
    workspace_path = folder / "workspace"
    workspace_path.mkdir(parents=True)
    for name, content in case.files.items():
        (workspace_path / name).write_text(content, encoding="utf-8")
    # Every initial input is protected except explicitly editable source code.
    editable = set(CONTRACTS[case.id].editable) if case.id in CONTRACTS else ({"calc.py"} if case.id == "repair_total" else set())
    hashes = {name:sha256_file(workspace_path / name) for name in case.files
              if name not in editable}
    _write_json(folder / "initial_manifest.json", {"case":manifest(case), "protected_hashes":hashes,
                                                   "comparison_id":comparison_id})
    started = time.perf_counter()
    runtime = None
    model = None
    registry = None
    prior_run_id = None
    prior_seed_calls = None
    timed_out = False
    environment_error = None
    environment_detail = None
    stage = "preflight"
    checks = {}
    mode = "fixtures" if fixture else "live"
    try:
        if not fixture:
            environment_error = await asyncio.to_thread(_missing_dependency, case)
        if environment_error is None:
            stage = "workspace_setup"
            workspace = Workspace(workspace_path)
            stage = "tool_registry"
            registry = _registry(case, workspace, fixture)
            stage = "store_setup"
            store = RunStore(workspace_path / ".kong" / "runs")
            async with httpx.AsyncClient(follow_redirects=False) as client:
                stage = "runtime_setup"
                model = ((variation_fixture_model(case.id) if case.id in CONTRACTS else
                          legacy_a3_fixture_model(case.id, exact_contract=task_set == "legacy12_contracts")
                          if task_set in {"legacy12_a3", "legacy12_contracts"} else
                          fixture_model("plan_approval" if case.category == "approval" else case.id))
                         if fixture else MeteredModel(config, client))
                if case.id in CONTRACTS and CONTRACTS[case.id].continuation:
                    if task_set in {"variation16_handoff", "variation16_v2", "variation16_contracts"}:
                        prior_model = ScriptedModel([
                            Act(actions=[Action(name="read_file", arguments={"path":"handoff.md"})]),
                            Act(actions=[Action(name="process_exec", arguments={"argv":["python","check.py"]})]),
                            NeedUserInput(question="The prior check failed. Continue in a later session."),
                        ])
                        prior = Runtime.new("Inspect the existing unfinished work and its check before handoff",
                                            prior_model, workspace, registry, store=store)
                        prior.state.turn_budget = 6
                        prior.state.hard_turn_limit = 6
                        await prior.run()
                        prior_seed_calls = prior.metrics.model_calls
                    else:
                        prior = Runtime.new("Previous session handoff", fixture_model("unknown_action_restore"),
                                            workspace, registry, store=store)
                        prior.state.status = Status.COMPLETED
                        prior.state.final_output = "Work handed off for a later session."
                        prior.history.append("human", 0, note="Prior session handoff was saved")
                        prior.save()
                    prior_run_id = prior.state.run_id
                    runtime = Runtime.new(case.goal, model, workspace, registry, mode=case.mode,
                                          store=store, thread_id=prior.thread.thread_id)
                else:
                    runtime = Runtime.new(case.goal, model, workspace, registry, mode=case.mode, store=store)
                runtime.state.hard_turn_limit = case.turn_limit
                runtime.state.turn_budget = case.turn_limit
                runtime.state.success_criteria = _criteria(case, task_set)
                if case.id == "unknown_action_restore":
                    runtime.history.append("action_intent", 0, action_id="a1",
                                           action=Action(name="write_file", arguments={"path":"canary.txt","content":"DUPLICATE"}).model_dump())
                    runtime.pending_action_id = "a1"
                    runtime.save()
                    runtime = Runtime.restore(store.load(runtime.state.run_id), model, workspace, registry, store=store)
                try:
                    stage = "runtime_run"
                    await asyncio.wait_for(runtime.run(), timeout=case.timeout_seconds)
                except TimeoutError:
                    timed_out = True
                    runtime.save()
            stage = "external_verifier"
            checks = (await verify_variation(case, workspace_path, runtime, hashes, sha256_file, prior_run_id=prior_run_id,
                                              prior_protocol="executed" if task_set in {"variation16_handoff", "variation16_v2", "variation16_contracts"} else "seeded",
                                              readback_policy="protected_check" if task_set in {"variation16_v2", "variation16_contracts"} else "direct")
                      if case.id in CONTRACTS else await verify(case, workspace_path, runtime, hashes, sha256_file))
    except Exception as exc:
        environment_error = "harness_exception:" + type(exc).__name__
        environment_detail = _safe_exception_location(exc, stage)
    finally:
        if registry is not None:
            try:
                await registry.aclose()
            except Exception as exc:
                environment_error = "cleanup_error:" + type(exc).__name__
                environment_detail = _safe_exception_location(exc, "cleanup")
    if runtime is None:
        status = "environment_error"
        runtime_status = None
        model_calls = 0
        usage = None
        call_records = []
        turns = 0
        error_completion = False
        stop_reason = None
    else:
        status = _status(case, runtime, checks, timed_out=timed_out, environment_error=environment_error)
        runtime_status = runtime.state.status.value
        model_calls = runtime.metrics.model_calls
        usage = runtime.metrics.tokens if runtime.metrics.usage_calls else None
        call_records = model.calls if isinstance(model, MeteredModel) else []
        turns = runtime.state.turn_count
        error_completion = runtime.state.status == Status.COMPLETED and not (checks and all(checks.values()))
        stop_reason = runtime.state.stop_reason
    failures = [name for name, passed in checks.items() if not passed]
    artifact_names = ([CONTRACTS[case.id].artifact, *CONTRACTS[case.id].editable] if case.id in CONTRACTS else
                      [name for name in ("summary.csv", "process.py", "calc.py", "research.json",
                                              "answer.txt", "result.txt", "saved.json", "proof.png")
                       if name not in hashes])
    artifacts = [{"path":name, "bytes":(workspace_path / name).stat().st_size,
                  "sha256":sha256_file(workspace_path / name)}
                 for name in dict.fromkeys(artifact_names) if (workspace_path / name).is_file()
                 and not (workspace_path / name).is_symlink()]
    audit_record = None
    try:
        audit_record = archive_fixture_trial(folder)
    except Exception as exc:
        environment_error = "audit_exception:" + type(exc).__name__
        environment_detail = _safe_exception_location(exc, "trial_audit")
        status = "environment_error"
    result = {"schema":EVAL_SCHEMA, "suite_id":suite_id, "comparison_id":comparison_id,
              "case":case.id, "category":case.category, "repetition":repeat, "mode":mode, "task_set":task_set,
              "implementation_sha256":implementation_sha256, "resource_fingerprint":resource_fingerprint,
              "fixture_simulations":fixture and bool({"terminal","mcp","browser"} & set(case.allowed_tools)),
              "score_scope":case.score_scope, "status":status, "runtime_status":runtime_status,
              "error_completion":error_completion, "checks":checks,
              "failure_reason":environment_error or ("timeout" if timed_out else ("checks_failed:" + ",".join(failures) if failures else stop_reason if status == "failed" else None)),
              "environment_detail":environment_detail,
              "run_id":runtime.state.run_id if runtime else None, "turns":turns,
              "model_calls":model_calls, "call_records":call_records, "usage":usage,
              "usage_complete":(runtime.metrics.usage_calls == model_calls and not runtime.metrics.legacy_unknown) if runtime and not fixture and model_calls else None,
              "model_errors":_model_error_categories(runtime) if runtime else [],
              "fatal_model_configuration": bool(runtime and any(e.type == "model_error" and e.payload.get("fatal_configuration")
                                                                  for e in runtime.history.events)),
              "failed_tools":_tool_failures(runtime) if runtime else [],
              "elapsed_seconds":round(time.perf_counter()-started,3), "stop_reason":stop_reason,
              "model":config.model if config else "scripted_fixture", "profile":profile if config else None,
              "human_intervention":{"measured":False,"count":None,"corrections":None,"rescues":None,"automatic_approval":False},
              "prior_run_id":prior_run_id,
              "prior_seed_calls":prior_seed_calls,
              "audit":audit_record,
              "artifacts":artifacts,
              "protected_hashes":hashes, "result_path":str(folder / "result.json")}
    _write_json(folder / "result.json", result)
    return result


async def run_suite(output: Path, *, case_ids: list[str] | None = None, repetitions: int = 3,
                    live: bool = False, config: ModelConfig | None = None, profile: str | None = None,
                    task_set: str = "legacy12"):
    """Run all requested trials. No retries, approvals or existing-directory reuse."""
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if live and config is None:
        raise ValueError("live mode requires an explicit model config")
    if task_set not in CASE_SETS:
        raise ValueError("Unknown task set")
    selected = [case for case in CASE_SETS[task_set] if case_ids is None or case.id in case_ids]
    if not selected:
        raise ValueError("No selected cases")
    if case_ids and set(case_ids) != {case.id for case in selected}:
        raise ValueError("Unknown case ID")
    mode = "live" if live else "fixtures"
    config_public = ({"model":config.model, "json_mode":config.json_mode,
                      "enable_thinking":config.enable_thinking, "timeout":config.timeout,
                      "max_tokens":config.max_tokens, "context_window_tokens":config.context_window_tokens,
                      "endpoint_fingerprint":hashlib.sha256(config.base_url.encode()).hexdigest()[:16],
                      "profile":profile} if config else {"model":"scripted_fixture"})
    resources = _behavior_resources()
    resource_fingerprint = _digest(resources)
    manifest_data = {"schema":EVAL_SCHEMA, "kong_version":kong.__version__, "mode":mode,
                     "task_set":task_set, "python":platform.python_version(), "platform":platform.system(),
                     "platform_release":platform.release(), "platform_machine":platform.machine(),
                     "continuation_protocol":("executed_failed_check_then_waiting_user" if task_set in {"variation16_handoff", "variation16_v2", "variation16_contracts"}
                                               else "synthetic_completed_handoff" if task_set == "variation16" else None),
                     "readback_policy":"protected_check" if task_set in {"variation16_v2", "variation16_contracts"} else "direct",
                     "configuration":config_public, "cases":[manifest(case) for case in selected],
                     "runtime_criteria":{case.id:[c.model_dump(mode="json") for c in _criteria(case, task_set)] for case in selected},
                     "repetitions":repetitions, "behavior_resources":resources,
                     "resource_fingerprint":resource_fingerprint,
                     "measurement_protocol":{"all_attempts_retained":True,"no_posthoc_rescoring":True,
                                             "automatic_human_intervention":"unmeasured",
                                             "actual_usage_only":True}}
    comparison_id = _digest(manifest_data)
    implementation_sha256 = _implementation_sha256()
    suite_id = uuid4().hex
    root = output.resolve() / suite_id
    root.mkdir(parents=True, exist_ok=False)
    _write_json(root / "manifest.json", {**manifest_data, "comparison_id":comparison_id,
                                         "implementation_sha256":implementation_sha256, "suite_id":suite_id,
                                         "created_at":datetime.now(timezone.utc).isoformat()})
    results = []
    planned = len(selected) * repetitions
    def summarize(stopped=None):
        result = _summary(results, mode, suite_id, comparison_id, implementation_sha256, task_set, resource_fingerprint)
        result.update(planned_runs=planned, recorded_runs=len(results), not_attempted=planned-len(results),
                      complete=len(results) == planned, stopped_reason=stopped)
        return result
    for case in selected:
        for repeat in range(1, repetitions+1):
            folder = root / "cases" / case.id / f"trial-{repeat:02d}"
            folder.mkdir(parents=True, exist_ok=False)
            result = await _trial(case, repeat, folder, fixture=not live, config=config,
                                  comparison_id=comparison_id, suite_id=suite_id, profile=profile,
                                  implementation_sha256=implementation_sha256,
                                  task_set=task_set, resource_fingerprint=resource_fingerprint)
            results.append(result)
            stopped = "fatal_model_configuration" if live and result.get("fatal_model_configuration") else None
            summary = summarize(stopped)
            _write_json(root / "summary.json", summary)
            print(json.dumps({"case":case.id,"repetition":repeat,"status":result["status"],
                              "model_calls":result["model_calls"]}, ensure_ascii=False), flush=True)
            if stopped:
                return root, summary
    return root, summarize()
