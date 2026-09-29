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
from kong.contracts import Action, Criterion, Status
from kong.integrations.config import MCPServer
from kong.models.compatible import CompatibleModel
from kong.runtime.loop import Runtime
from kong.storage import RunStore
from kong.tools.defaults import default_registry
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace

from .fixtures import FixtureBrowser, FixtureMCP, FixtureTerminal, fixture_model
from .specs import CASES, Case
from .verifiers import verify

EVAL_SCHEMA = "kong-eval-1"


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


def _write_json(path: Path, data) -> None:
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    pending.replace(path)


def manifest(case: Case) -> dict:
    return {
        "id": case.id, "category": case.category, "goal": case.goal,
        "initial_environment": {"files": case.files, "fresh_workspace": True, "fresh_runtime_store": True,
                                "fresh_model_client": True, "fresh_tool_registry": True},
        "allowed_tools": list(case.allowed_tools), "success_condition": case.success,
        "time_budget_seconds": case.timeout_seconds, "turn_budget": case.turn_limit,
        "mode": case.mode.value, "score_scope": case.score_scope,
        "source_scope": "fixed_local_snapshot_only" if case.id == "source_tomllib" else None,
    }


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


def _criteria(case: Case):
    output = {"data":"summary.csv", "repair":"calc.py", "source_research":"research.json",
              "terminal":"answer.txt", "mcp":"result.txt", "browser":"saved.json", "tool_error":"result.txt"}.get(case.category)
    result = [Criterion(id="artifact", description="Requested file exists", kind="file_exists", path=output)] if output else []
    if case.category in {"data", "repair"}:
        result.append(Criterion(id="execution", description="Program ran after edit", kind="tool_success",
                                tool_name="process_exec", after_last_write=True))
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
        return "expected_approval" if case.id == "plan_approval" else "success"
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
        if "Model service HTTP" in value:
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


def _summary(results, mode, suite_id, comparison_id, implementation_sha256):
    counts = {s:sum(r["status"] == s for r in results) for s in
              ("success","failed","expected_approval","environment_error","timeout")}
    scored = [r for r in results if r["score_scope"] == "model"] if mode == "live" else []
    eligible = [r for r in scored if r["status"] not in {"environment_error"}]
    claims = [r for r in results if r["score_scope"] == "model" and r["runtime_status"] == "completed"]
    incorrect = sum(r["error_completion"] for r in claims)
    return {"schema":EVAL_SCHEMA, "suite_id":suite_id, "mode":mode, "comparison_id":comparison_id,
            "implementation_sha256":implementation_sha256,
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
            "completion_rate_scope":"Model-scored trials in the stated mode; mechanism-only trials excluded. Fixture claims do not measure model reliability.",
            "human_intervention":{"measured":False,"count":None,"note":"No operator rescue or automatic approval during these automated trials; real supervision burden was not measured."},
            "results":[{"case":r["case"], "repetition":r["repetition"], "status":r["status"],
                        "model_calls":r["model_calls"], "result":r["result_path"]} for r in results]}


async def _trial(case: Case, repeat: int, folder: Path, *, fixture: bool, config: ModelConfig | None,
                 comparison_id: str, suite_id: str, profile: str | None, implementation_sha256: str):
    workspace_path = folder / "workspace"
    workspace_path.mkdir(parents=True)
    for name, content in case.files.items():
        (workspace_path / name).write_text(content, encoding="utf-8")
    # calc.py is the explicitly editable repair target; every other source is protected.
    hashes = {name:sha256_file(workspace_path / name) for name in case.files
              if not (case.id == "repair_total" and name == "calc.py")}
    _write_json(folder / "initial_manifest.json", {"case":manifest(case), "protected_hashes":hashes,
                                                   "comparison_id":comparison_id})
    started = time.perf_counter()
    runtime = None
    model = None
    registry = None
    timed_out = False
    environment_error = None
    checks = {}
    mode = "fixtures" if fixture else "live"
    try:
        if not fixture:
            environment_error = await asyncio.to_thread(_missing_dependency, case)
        if environment_error is None:
            workspace = Workspace(workspace_path)
            registry = _registry(case, workspace, fixture)
            store = RunStore(workspace_path / ".kong" / "runs")
            async with httpx.AsyncClient(follow_redirects=False) as client:
                model = fixture_model(case.id) if fixture else MeteredModel(config, client)
                runtime = Runtime.new(case.goal, model, workspace, registry, mode=case.mode, store=store)
                runtime.state.hard_turn_limit = case.turn_limit
                runtime.state.turn_budget = case.turn_limit
                runtime.state.success_criteria = _criteria(case)
                if case.id == "unknown_action_restore":
                    runtime.history.append("action_intent", 0, action_id="a1",
                                           action=Action(name="write_file", arguments={"path":"canary.txt","content":"DUPLICATE"}).model_dump())
                    runtime.pending_action_id = "a1"
                    runtime.save()
                    runtime = Runtime.restore(store.load(runtime.state.run_id), model, workspace, registry, store=store)
                try:
                    await asyncio.wait_for(runtime.run(), timeout=case.timeout_seconds)
                except TimeoutError:
                    timed_out = True
                    runtime.save()
            checks = await verify(case, workspace_path, runtime, hashes, sha256_file)
    except Exception as exc:
        environment_error = "harness_exception:" + type(exc).__name__
    finally:
        if registry is not None:
            try:
                await registry.aclose()
            except Exception as exc:
                environment_error = "cleanup_error:" + type(exc).__name__
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
    result = {"schema":EVAL_SCHEMA, "suite_id":suite_id, "comparison_id":comparison_id,
              "case":case.id, "category":case.category, "repetition":repeat, "mode":mode,
              "implementation_sha256":implementation_sha256,
              "fixture_simulations":fixture and bool({"terminal","mcp","browser"} & set(case.allowed_tools)),
              "score_scope":case.score_scope, "status":status, "runtime_status":runtime_status,
              "error_completion":error_completion, "checks":checks,
              "failure_reason":environment_error or ("timeout" if timed_out else ("checks_failed:" + ",".join(failures) if failures else stop_reason if status == "failed" else None)),
              "run_id":runtime.state.run_id if runtime else None, "turns":turns,
              "model_calls":model_calls, "call_records":call_records, "usage":usage,
              "usage_complete":(runtime.metrics.usage_calls == model_calls and not runtime.metrics.legacy_unknown) if runtime and not fixture and model_calls else None,
              "model_errors":_model_error_categories(runtime) if runtime else [],
              "failed_tools":_tool_failures(runtime) if runtime else [],
              "elapsed_seconds":round(time.perf_counter()-started,3), "stop_reason":stop_reason,
              "model":config.model if config else "scripted_fixture", "profile":profile if config else None,
              "human_intervention":{"measured":False,"count":None,"automatic_approval":False},
              "protected_hashes":hashes, "result_path":str(folder / "result.json")}
    _write_json(folder / "result.json", result)
    return result


async def run_suite(output: Path, *, case_ids: list[str] | None = None, repetitions: int = 3,
                    live: bool = False, config: ModelConfig | None = None, profile: str | None = None):
    """Run all requested trials. No retries, approvals or existing-directory reuse."""
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if live and config is None:
        raise ValueError("live mode requires an explicit model config")
    selected = [case for case in CASES if case_ids is None or case.id in case_ids]
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
    manifest_data = {"schema":EVAL_SCHEMA, "kong_version":kong.__version__, "mode":mode,
                     "python":platform.python_version(), "platform":platform.system(),
                     "configuration":config_public, "cases":[manifest(case) for case in selected],
                     "repetitions":repetitions}
    comparison_id = _digest(manifest_data)
    implementation_sha256 = _implementation_sha256()
    suite_id = uuid4().hex
    root = output.resolve() / suite_id
    root.mkdir(parents=True, exist_ok=False)
    _write_json(root / "manifest.json", {**manifest_data, "comparison_id":comparison_id,
                                         "implementation_sha256":implementation_sha256, "suite_id":suite_id,
                                         "created_at":datetime.now(timezone.utc).isoformat()})
    results = []
    for case in selected:
        for repeat in range(1, repetitions+1):
            folder = root / "cases" / case.id / f"trial-{repeat:02d}"
            folder.mkdir(parents=True, exist_ok=False)
            result = await _trial(case, repeat, folder, fixture=not live, config=config,
                                  comparison_id=comparison_id, suite_id=suite_id, profile=profile,
                                  implementation_sha256=implementation_sha256)
            results.append(result)
            _write_json(root / "summary.json", _summary(results, mode, suite_id, comparison_id, implementation_sha256))
            print(json.dumps({"case":case.id,"repetition":repeat,"status":result["status"],
                              "model_calls":result["model_calls"]}, ensure_ascii=False), flush=True)
    return root, _summary(results, mode, suite_id, comparison_id, implementation_sha256)
